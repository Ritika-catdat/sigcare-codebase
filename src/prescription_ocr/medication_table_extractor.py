import re
from collections import Counter
from dataclasses import replace

from .output_contract import transform_date
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


def merge_into(record, fields, provenance, separators=None):
    """Append a row that continues the record's row: a row split by a page break, or the next
    printed line of a wrapped row. separators gives '' for a word broken across the lines."""
    for field, value in fields.items():
        if not value:
            continue
        sep = (separators or {}).get(field, ' ')
        record[field] = f'{record[field]}{sep}{value}' if record.get(field) else value
        before = record['_provenance'].get(field)
        record['_provenance'][field] = {'components': [before, provenance[field]]} if before else provenance[field]


# Lines closer than this many word heights are lines of one wrapped cell, not two rows:
# printed lines in a cell sit about a third of a line apart, while ruled rows also have the
# ruling line and the cell padding between them.
WRAP_GAP = .55


def _words(doc, cells):
    return [doc.index[i] for c in cells if c for i in c.word_ids if i in doc.index]


def is_wrapped_line(doc, upper_cells, cells, record, fields):
    """True when this row is the next printed line of the record's row, not a new medicine.

    Textract sometimes cuts one ruled row into a row per printed line when its cells wrap
    ("Laxido" / "Orange oral" / "powder" / ...). Such a line fills fewer columns than the
    record, since one-line columns (quantity, medication type, ...) are already complete,
    and either finishes a date left incomplete above ("13" / "Oct" / "2026") or sits right
    under the line above without the gap that a ruling line and cell padding leave.
    """
    filled = {f for f, v in fields.items() if v}
    before = {f for f in fields if record.get(f)}
    if not filled or len(filled) >= len(before) or len(filled - before) > 1:
        return False
    if any(record.get(f) and fields.get(f) and not transform_date(record[f]) for f in ('start_date', 'end_date')):
        return True
    # A row with its own complete date, or one filling most of the record's columns, is the
    # next medicine: "Amlodipine 10mg tablets | 1 tablet | ... | 28 Oct 2026" right under
    # "Orlistat ..." whose notes column made that row one field longer.
    if any(fields.get(f) and transform_date(fields[f]) for f in ('start_date', 'end_date')):
        return False
    if 2 * len(filled) > len(before):
        return False
    upper = [doc.reading_box(w) for w in _words(doc, upper_cells or [])]
    lower = [doc.reading_box(w) for w in _words(doc, cells)]
    heights = sorted(b.get('Height', 0) for b in upper + lower)
    if not upper or not lower or not heights[len(heights) // 2]:
        return False
    gap = min(b.get('Top', 0) for b in lower) - max(b.get('Top', 0) + b.get('Height', 0) for b in upper)
    return gap < WRAP_GAP * heights[len(heights) // 2]


# Columns that hold one short value per medicine, and the word that ends that value.
# Two complete values in one cell mean Textract put two medicines in one row.
VALUE_END = {
    'start_date': re.compile(r'^\d{4}[.,]?$'),
    'end_date': re.compile(r'^\d{4}[.,]?$'),
    'days_duration': re.compile(r'(?i)^(?:days?|weeks?|months?)(?:\(s\))?[.,]?$'),
    'possession_interval': re.compile(r'(?i)^(?:days?|weeks?|months?)(?:\(s\))?[.,]?$'),
}
# Words a value must contain besides its last word: a month in a date, a number in a duration.
VALUE_BODY = {
    'start_date': re.compile(r'(?i)^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'),
    'end_date': re.compile(r'(?i)^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'),
    'days_duration': re.compile(r'^\d+$'),
    'possession_interval': re.compile(r'^\d+$'),
}
# Medication type values start with a capital ("Regular", "When required", "As required").
TYPE_START = re.compile(r'^[A-Z][a-z]{2,}')


def _reading_words(doc, cell):
    """The cell's words as (box, word) in reading order on the upright page."""
    pieces = []
    for i in cell.word_ids if cell else []:
        if i in doc.index:
            doc._insert_in_reading_order(pieces, (doc.reading_box(doc.index[i]), doc.index[i]))
    return pieces


def _value_starts(doc, cell, field):
    """Boxes of the first word of the 2nd, 3rd ... value in a one-value-per-medicine cell,
    or None when the cell holds a single value (or values that are not all complete)."""
    pieces = _reading_words(doc, cell)
    texts = [w.get('Text', '') for _, w in pieces]
    if field == 'medication_type':
        starts = [i for i, t in enumerate(texts) if i and TYPE_START.match(t)]
        return [pieces[i][0] for i in starts] or None
    end, body = VALUE_END[field], VALUE_BODY[field]
    starts, begin = [], 0
    for i, text in enumerate(texts):
        if end.match(text) and i > begin:
            if not any(body.match(t) for t in texts[begin:i]):
                return None
            if begin:
                starts.append(pieces[begin][0])
            begin = i + 1
    if begin != len(texts):
        return None  # words after the last complete value
    return starts or None


def split_merged_row(doc, cells, mapping):
    """Cut a row holding two (or more) medicines into one row per medicine.

    Textract sometimes misses the ruling line between two rows and returns both
    medicines in one row ("Mirtazapine ... Lansoprazole ...", "Regular Regular"). When at
    least two one-value columns (start date, duration, medication type, interval) each hold
    the same number of complete values, and each next value starts at the same height in
    all of them (cells of one row start level), every cell is cut at that height.
    """
    starts = {}
    for field in [*VALUE_END, 'medication_type']:
        ix = mapping.get(field)
        cell = cells[ix] if ix is not None and ix < len(cells) else None
        found = _value_starts(doc, cell, field) if cell else None
        if found:
            starts[field] = found
    counts = Counter(len(v) for v in starts.values())
    if not counts:
        return [cells]
    splits, agreeing = counts.most_common(1)[0]
    if agreeing < 2:
        return [cells]
    columns = [v for v in starts.values() if len(v) == splits]
    cuts = []
    for k in range(splits):
        boxes = [col[k] for col in columns]
        tops = [b.get('Top', 0) for b in boxes]
        height = sorted(b.get('Height', 0) for b in boxes)[len(boxes) // 2]
        if not height or max(tops) - min(tops) > height:
            return [cells]
        cuts.append(min(tops) - .3 * height)
    parts = [[] for _ in range(splits + 1)]
    for cell in cells:
        if cell is None:
            for part in parts:
                part.append(None)
            continue
        def part_of(box):
            return sum(box.get('Top', 0) + box.get('Height', 0) / 2 >= cut for cut in cuts)
        texts, ids = [[] for _ in parts], [[] for _ in parts]
        # The text pieces keep words already given back to their own column.
        for box, text in cell.pieces if cell.pieces is not None else [
                (b, w.get('Text', '')) for b, w in _reading_words(doc, cell)]:
            texts[part_of(box)].append((box, text))
        for box, word in _reading_words(doc, cell):
            ids[part_of(box)].append(word['Id'])
        for part, text, word_ids in zip(parts, texts, ids):
            part.append(replace(cell, word_ids=word_ids, pieces=text, text=doc._join_lines(text, cell.bbox)))
    return parts


def handwritten_words(doc, cell):
    """Handwritten words in a printed table cell, e.g. a "7" written beside a struck-out "28".

    Pen strokes read as punctuation ("-", "~") are strike-through marks, not values.
    """
    if not cell:
        return []
    words = (doc.index.get(i) for i in cell.word_ids)
    return [w.get('Text', '') for w in words
            if w and w.get('TextType') == 'HANDWRITING' and re.search(r'[A-Za-z0-9]', w.get('Text', ''))]


def _row_numbers(doc, cells, handwritten):
    words = (doc.index.get(i) for c in cells if c for i in c.word_ids)
    return {n for w in words if w and (w.get('TextType') == 'HANDWRITING') == handwritten
            for n in re.findall(r'\d+', w.get('Text', ''))}


def split_struck_numbers(doc, cells, fields, handwriting):
    """Split a struck-out printed number from the number written beside it.

    OCR can read a printed "28" crossed out with a "7" written right after it as one
    handwritten word "287". It is split when the front part is printed elsewhere in the
    row ("28 day(s)") and the rest appears elsewhere in the row too (the "7" written in
    the next column, or "7 capsules"), so "287 capsule" becomes "28 7 capsule" with only
    the "7" handwritten.
    """
    printed = _row_numbers(doc, cells, False)
    numbers = printed | _row_numbers(doc, cells, True)
    for field, words in handwriting.items():
        for i, word in enumerate(words):
            if not word.isdigit() or len(word) < 2:
                continue
            # Elsewhere in the row: drop the merged word itself from the handwritten numbers.
            others = numbers - {word}
            splits = [(word[:k], word[k:]) for k in range(1, len(word))
                      if word[:k] in printed and word[k:] in others]
            if not splits:
                continue
            front, rest = max(splits, key=lambda s: len(s[0]))
            words[i] = rest
            fields[field] = re.sub(rf'(?<!\d){word}(?!\d)', f'{front} {rest}', fields[field], count=1)


def merge_handwriting(record, handwriting):
    for field, words in handwriting.items():
        record.setdefault('_handwritten', {}).setdefault(field, []).extend(words)


def _separator(doc, upper, lower):
    """'' when the printer broke one word across the two lines ("Aripiprazol" / "e"), else ' '."""
    if not upper or not lower or not upper.word_ids or not lower.word_ids:
        return ' '
    a, b = doc.index.get(upper.word_ids[-1]), doc.index.get(lower.word_ids[0])
    if not a or not b:
        return ' '
    joined = doc._join_lines([(doc.reading_box(a), a.get('Text', '')), (doc.reading_box(b), b.get('Text', ''))],
                             upper.bbox)
    return ' ' if ' ' in joined else ''


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
        # The last emitted record of this table, the cells of its latest line and, per
        # field, the cell that last added text (to join a word broken across lines).
        open_record, upper_cells, field_cells = None, None, {}
        for r in sorted({r for r,_ in grid}):
            if r <= header_row:
                continue
            row = cells_in_row(grid, r)
            headers = [c.text if c else '' for c in row]
            if len(map_headers(headers, TABLES[kind])) >= max(2, len(mapping)//2) or is_header_fragment(row):
                continue  # repeated header, or the wrapped second line of the header
            # Two medicines Textract returned as one row are read as two rows.
            for cells in split_merged_row(doc, row, mapping):
                fields, provenance, row_cells, handwriting = {}, {}, {}, {}
                for field in TABLES[kind]:
                    ix = mapping.get(field)
                    cell = cells[ix] if ix is not None and ix < len(cells) else None
                    # Cells holding only ruling lines or stray marks ("|", "-") count as empty.
                    fields[field] = cell.text.strip() if cell and norm(cell.text) else None
                    provenance[field] = doc.provenance(cell.block_ids, cell.word_ids, cell.page) if fields[field] else None
                    if fields[field]:
                        row_cells[field] = cell
                        if handwritten_words(doc, cell):
                            handwriting[field] = handwritten_words(doc, cell)
                if not any(v for v in fields.values()):
                    continue
                split_struck_numbers(doc, cells, fields, handwriting)
                if open_record is not None and is_wrapped_line(doc, upper_cells, cells, open_record, fields):
                    # One printed row that Textract cut into a row per line: put it back together.
                    merge_into(open_record, fields, provenance,
                               {f: _separator(doc, field_cells.get(f), c) for f, c in row_cells.items()})
                    merge_handwriting(open_record, handwriting)
                    upper_cells = cells
                    field_cells.update(row_cells)
                    continue
                last = result[kind][-1] if result[kind] else None
                partial = not fields.get('drug_name') and not any(fields.get(f) for f in CRITICAL_FIELDS[kind])
                continues = partial and first_row and last and last['_source']['page'] < table.get('Page', 1)
                first_row = False
                if continues:
                    # Same table carried over a page break: complete the previous record instead
                    # of emitting a second, mostly blank one.
                    merge_into(last, fields, provenance)
                    merge_handwriting(last, handwriting)
                    continue
                if partial:
                    # No medicine/allergy and nothing to attach it to (e.g. an empty table whose
                    # rows continue on the next page): kept for audit, not emitted as a record.
                    meta.setdefault('skipped_rows', []).append(' | '.join(v for v in fields.values() if v))
                    continue
                record = {**fields, '_provenance': provenance, '_handwritten': handwriting, '_columns': set(mapping),
                          '_source': {'page':table.get('Page',1), 'table_id':table['Id'], 'row':r}}
                result[kind].append(record)
                meta['row_count'] += 1
                open_record, upper_cells, field_cells = record, cells, row_cells
    # Review reasons are computed once all pages are read, so a record completed by a
    # continuation row is judged on its merged values.
    for kind, records in result.items():
        for record in records:
            # A layout without the column (no "Dose" on some forms; the script notes carry
            # it) is not a missing value.
            columns = record.pop('_columns', None)
            missing = [f for f in CRITICAL_FIELDS[kind] if not record.get(f) and (columns is None or f in columns)]
            low_conf = [f for f, p in record['_provenance'].items()
                        if p and p.get('ocr_confidence') is not None and p['ocr_confidence'] < cfg.min_cell_confidence]
            record['_review_reasons'] = ([f'Missing critical fields: {", ".join(sorted(missing))}'] if missing else []) + ([f'Low OCR confidence: {", ".join(low_conf)}'] if low_conf else [])
            # A value written by hand in a printed cell (often a correction over a struck-out
            # printed value) is kept in the output, and a person checks which value applies.
            hand = record.get('_handwritten') or {}
            if hand:
                record['_review_reasons'].append('Handwritten text in table: ' + ', '.join(
                    f'{field} "{" ".join(words)}"' for field, words in hand.items()))
            if record['_review_reasons']:
                source = record['_source']
                review.append(f'{kind} page {source["page"]} row {source["row"]}: {"; ".join(record["_review_reasons"])}')
    return result, table_meta, review
