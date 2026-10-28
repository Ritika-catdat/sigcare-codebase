from .schemas import TABLES, CRITICAL_FIELDS, map_headers, classify, match_score, norm

# Every word used in a column header. A row made only of these words is the second
# line of a wrapped header (e.g. "Notes | date | duration | type"), not a medicine.
HEADER_WORDS = {w for schema in TABLES.values() for aliases in schema.values()
                for alias in aliases for w in norm(alias).split()} | {'printed', 'by'}
# Columns that make an unrecognised table worth flagging rather than ignoring.
MEDICINE_COLUMNS = {'drug_name', 'administration_dose_time_description', 'allergy_or_sensitivity'}


def is_patient_table(grid):
    vals = [c.text for c in grid.values() if c.text]
    labels = ('date of birth', 'prisoner number', 'landing cell', 'patient name')
    return sum(any(match_score(v, [label]) >= .75 for v in vals) for label in labels) >= 2


def cells_in_row(grid, r):
    maxcol = max((c for _, c in grid), default=0)
    return [grid.get((r, col)) for col in range(1, maxcol + 1)]


def is_header_fragment(cells):
    texts = [norm(c.text) for c in cells if c and norm(c.text)]
    return bool(texts) and all(set(t.split()) <= HEADER_WORDS for t in texts)


def merge_into(record, fields, provenance):
    """Append a row that continues the previous page's last row (a row split by a page break)."""
    for field, value in fields.items():
        if not value:
            continue
        record[field] = f'{record[field]} {value}' if record.get(field) else value
        before = record['_provenance'].get(field)
        record['_provenance'][field] = {'components': [before, provenance[field]]} if before else provenance[field]


def header_for(grid, title, context):
    rows = sorted({r for r, _ in grid})
    candidates = []
    for offset, r in enumerate(rows[:4]):
        row = cells_in_row(grid, r)
        headers = [c.text if c else '' for c in row]
        if not any(headers):
            continue
        score, kind, mapping = classify(headers, title, context)
        candidates.append((score, kind, mapping, r))
        if offset < len(rows[:4]) - 1:
            row2 = cells_in_row(grid, rows[offset+1])
            combined = [(headers[i] + ' ' + (row2[i].text if i < len(row2) and row2[i] else '')).strip()
                        for i in range(len(headers))]
            score2, kind2, mapping2 = classify(combined, title, context)
            candidates.append((score2, kind2, mapping2, rows[offset+1]))
    return max(candidates, key=lambda x:x[0]) if candidates else (0, None, {}, 0)


def extract_tables(doc, cfg):
    result = {kind: [] for kind in TABLES}
    table_meta = []
    review = []
    previous = None
    for table in doc.tables:
        grid = doc.table_grid(table)
        if not grid or is_patient_table(grid):
            continue
        title = ' '.join(doc.text_of(doc.index[i]) for i in doc.related(table, 'TABLE_TITLE') if i in doc.index)
        context = ' '.join(l.get('Text', '') for l in doc.adjacent_lines(table))
        score, kind, mapping, header_row = header_for(grid, title, context)
        if score < cfg.min_table_header_score:
            # Headerless continuation is allowed only when the prior classified table is
            # from an earlier page and column counts are compatible.
            columns = max(c for _, c in grid)
            if previous and table.get('Page', 1) > previous['page'] and columns == previous['columns']:
                kind = previous['kind']
                mapping = previous['mapping']
                header_row = 0
                score = previous['score'] * .75
            else:
                # Only a table with a medicine/allergy column is worth a reviewer's time. Other
                # boxes on the form ("Clinically screened by", "Medication Possession Status")
                # are not part of the output and are ignored.
                medicine_like = bool(set(mapping) & MEDICINE_COLUMNS)
                if medicine_like:
                    review.append(f'Unclassified table on page {table.get("Page",1)} (block {table.get("Id")})')
                # First rows as text so a reviewer can see what the table was without the raw OCR.
                preview = [' | '.join(c.text if c else '' for c in cells_in_row(grid, r))
                           for r in sorted({r for r, _ in grid})[:3]]
                table_meta.append({'table_id':table.get('Id'), 'page':table.get('Page'),
                                   'type':None if medicine_like else 'ignored',
                                   'header_score':round(score,3), 'preview':preview})
                previous = None
                continue
        columns = max(c for _, c in grid)
        previous = {'kind':kind,'mapping':mapping,'columns':columns,'page':table.get('Page',1),'score':score}
        meta = {'table_id':table['Id'],'page':table.get('Page',1),'type':kind,
                'header_score':round(score,3), 'row_count':0}
        table_meta.append(meta)
        first_row = True
        for r in sorted({r for r,_ in grid}):
            if r <= header_row:
                continue
            cells = cells_in_row(grid, r)
            headers = [c.text if c else '' for c in cells]
            if len(map_headers(headers, TABLES[kind])) >= max(2, len(mapping)//2) or is_header_fragment(cells):
                continue  # repeated header, or the wrapped second line of the header
            fields, provenance = {}, {}
            for field in TABLES[kind]:
                ix = mapping.get(field)
                cell = cells[ix] if ix is not None and ix < len(cells) else None
                # Cells holding only ruling lines or stray marks ("|", "-") count as empty.
                fields[field] = cell.text.strip() if cell and norm(cell.text) else None
                provenance[field] = doc.provenance(cell.block_ids, cell.word_ids, cell.page) if fields[field] else None
            if not any(v for v in fields.values()):
                continue
            last = result[kind][-1] if result[kind] else None
            partial = not fields.get('drug_name') and not any(fields.get(f) for f in CRITICAL_FIELDS[kind])
            continues = partial and first_row and last and last['_source']['page'] < table.get('Page', 1)
            first_row = False
            if continues:
                # Same table carried over a page break: complete the previous record instead
                # of emitting a second, mostly blank one.
                merge_into(last, fields, provenance)
                continue
            if partial:
                # No medicine/allergy and nothing to attach it to (e.g. an empty table whose
                # rows continue on the next page): kept for audit, not emitted as a record.
                meta.setdefault('skipped_rows', []).append(' | '.join(v for v in fields.values() if v))
                continue
            record = {**fields, '_provenance': provenance, '_source': {'page':table.get('Page',1), 'table_id':table['Id'], 'row':r}}
            result[kind].append(record)
            meta['row_count'] += 1
    # Review reasons are computed once all pages are read, so a record completed by a
    # continuation row is judged on its merged values.
    for kind, records in result.items():
        for record in records:
            missing = [f for f in CRITICAL_FIELDS[kind] if not record.get(f)]
            low_conf = [f for f, p in record['_provenance'].items()
                        if p and p.get('ocr_confidence') is not None and p['ocr_confidence'] < cfg.min_cell_confidence]
            record['_review_reasons'] = ([f'Missing critical fields: {", ".join(sorted(missing))}'] if missing else []) + ([f'Low OCR confidence: {", ".join(low_conf)}'] if low_conf else [])
            if record['_review_reasons']:
                source = record['_source']
                review.append(f'{kind} page {source["page"]} row {source["row"]}: {"; ".join(record["_review_reasons"])}')
    return result, table_meta, review
