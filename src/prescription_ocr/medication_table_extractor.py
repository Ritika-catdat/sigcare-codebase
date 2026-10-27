from .schemas import TABLES, CRITICAL_FIELDS, map_headers, classify, match_score, norm


def is_patient_table(grid):
    vals = [c.text for c in grid.values() if c.text]
    labels = ('date of birth', 'prisoner number', 'landing cell', 'patient name')
    return sum(any(match_score(v, [label]) >= .75 for v in vals) for label in labels) >= 2


def cells_in_row(grid, r):
    maxcol = max((c for _, c in grid), default=0)
    return [grid.get((r, col)) for col in range(1, maxcol + 1)]


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
                review.append(f'Unclassified table on page {table.get("Page",1)} (block {table.get("Id")})')
                table_meta.append({'table_id':table.get('Id'), 'page':table.get('Page'), 'type':None, 'header_score':round(score,3)})
                previous = None
                continue
        columns = max(c for _, c in grid)
        previous = {'kind':kind,'mapping':mapping,'columns':columns,'page':table.get('Page',1),'score':score}
        meta = {'table_id':table['Id'],'page':table.get('Page',1),'type':kind,
                'header_score':round(score,3), 'row_count':0}
        table_meta.append(meta)
        for r in sorted({r for r,_ in grid}):
            if r <= header_row:
                continue
            cells = cells_in_row(grid, r)
            headers = [c.text if c else '' for c in cells]
            if len(map_headers(headers, TABLES[kind])) >= max(2, len(mapping)//2):
                continue  # repeated header
            fields, provenance = {}, {}
            for field in TABLES[kind]:
                ix = mapping.get(field)
                cell = cells[ix] if ix is not None and ix < len(cells) else None
                fields[field] = cell.text.strip() if cell and cell.text.strip() else None
                provenance[field] = doc.provenance(cell.block_ids, cell.word_ids, cell.page) if cell and cell.text else None
            if not any(v for v in fields.values()):
                continue
            record = {**fields, '_provenance': provenance, '_source': {'page':table.get('Page',1), 'table_id':table['Id'], 'row':r}}
            missing = [f for f in CRITICAL_FIELDS[kind] if not fields.get(f)]
            low_conf = [f for f, p in provenance.items() if p and p['ocr_confidence'] is not None and p['ocr_confidence'] < cfg.min_cell_confidence]
            record['_review_reasons'] = ([f'Missing critical fields: {", ".join(sorted(missing))}'] if missing else []) + ([f'Low OCR confidence: {", ".join(low_conf)}'] if low_conf else [])
            result[kind].append(record)
            meta['row_count'] += 1
            if record['_review_reasons']:
                review.append(f'{kind} page {table.get("Page",1)} row {r}: {"; ".join(record["_review_reasons"])}')
    return result, table_meta, review
