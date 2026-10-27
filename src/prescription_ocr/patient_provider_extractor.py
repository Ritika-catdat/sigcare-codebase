import re
from .schemas import PATIENT, match_score, norm


def _value(value, provenance):
    return {'value': value.strip() if isinstance(value, str) and value.strip() else None,
            'provenance': provenance}


def extract_patient(doc):
    output = {k: _value(None, None) for k in PATIENT}
    candidates = []
    for pair in doc.form_pairs():
        b = pair['value_blocks'][0] if pair['value_blocks'] else pair['key_block']
        candidates.append((pair['key'], pair['value'], doc.provenance([v['Id'] for v in pair['value_blocks']] or [b['Id']]), 2))
    # Two-column labeled table, or OCR-detected multicolumn patient table.
    for table in doc.tables:
        grid = doc.table_grid(table)
        for (r, c), cell in grid.items():
            if not cell.text:
                continue
            next_cell = grid.get((r, c + 1))
            if next_cell and next_cell.text:
                candidates.append((cell.text, next_cell.text, doc.provenance(next_cell.block_ids, next_cell.word_ids, next_cell.page), 2))
    # Inline label: value; restrict to recognized labels and do not use medication-table rows as patient identifiers.
    for line in doc.lines():
        txt = line.get('Text', '')
        if ':' in txt:
            key, value = txt.split(':', 1)
            candidates.append((key, value, doc.provenance([line['Id']]), 1))
    for field, aliases in PATIENT.items():
        ranked = sorted(((match_score(key, aliases), priority, value, prov)
                         for key, value, prov, priority in candidates if value.strip()),
                        key=lambda x: (x[0], x[1]), reverse=True)
        if ranked and ranked[0][0] >= 0.76:
            output[field] = _value(ranked[0][2], ranked[0][3])
    return output


def extract_provider(doc):
    # Heuristic only: header layout can differ by institution.
    first_page = min(doc.pages, default=1)
    lines = [l for l in doc.lines(first_page) if doc.bbox(l).get('Top', 1) < 0.27]
    lines = [l for l in lines if l.get('Text', '').strip()]
    ignore = ('patient name', 'date of birth', 'prisoner number', 'landing cell', 'in possession', 'not in possession')
    candidate = [l for l in lines if not any(p in norm(l.get('Text', '')) for p in ignore)]
    name = candidate[0] if candidate else None
    address_lines = candidate[1:4] if name else []
    # Avoid interpreting a patient label as the hospital address.
    address_lines = [l for l in address_lines if not any(match_score(l.get('Text','').split(':')[0], aliases) > .8 for aliases in PATIENT.values())]
    return {'name': _value(name.get('Text') if name else None,
                            doc.provenance([name['Id']]) if name else None),
            'address': _value(' '.join(l.get('Text', '') for l in address_lines),
                               doc.provenance([l['Id'] for l in address_lines]) if address_lines else None)}
