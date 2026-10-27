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
    # Some prison forms use a single label/value cell ("Landing: A2") or
    # have the value immediately below the label rather than to its right.
    for table in doc.tables:
        grid = doc.table_grid(table)
        for (r, c), cell in grid.items():
            if not cell.text:
                continue
            if ':' in cell.text:
                label, value = cell.text.split(':', 1)
                if value.strip():
                    candidates.append((label, value, doc.provenance(cell.block_ids, cell.word_ids, cell.page), 3))
            below = grid.get((r + 1, c))
            if below and below.text and any(match_score(cell.text, aliases) >= .9 for aliases in PATIENT.values()):
                candidates.append((cell.text, below.text, doc.provenance(below.block_ids, below.word_ids, below.page), 1))
    for field, aliases in PATIENT.items():
        ranked = sorted(((match_score(key, aliases), priority, value, prov)
                         for key, value, prov, priority in candidates if value.strip()),
                        key=lambda x: (x[0], x[1]), reverse=True)
        if ranked and ranked[0][0] >= 0.76:
            output[field] = _value(ranked[0][2], ranked[0][3])
    # Landing and Cell are often separate labels in the patient header.
    # Combine them when both are present, without overwriting an explicit
    # "Landing / Cell" value already identified above.
    def best(label):
        ranked = sorted(((match_score(key, [label]), value, prov)
                         for key, value, prov, priority in candidates if value.strip()), key=lambda x: x[0], reverse=True)
        return (ranked[0][1], ranked[0][2]) if ranked and ranked[0][0] >= .88 else (None, None)
    landing, landing_prov = best('landing')
    cell, cell_prov = best('cell')
    explicit = any(match_score(k, ['landing cell', 'landing / cell']) >= .95 and v.strip()
                   for k, v, _, _ in candidates)
    if not explicit and landing and cell:
        output['landing_cell'] = _value(f'{landing} / {cell}', {'components': [landing_prov, cell_prov]})
    elif not explicit and landing:
        output['landing_cell'] = _value(landing, landing_prov)
    output['landing'] = _value(landing, landing_prov)
    output['cell'] = _value(cell, cell_prov)
    return output


def extract_provider(doc):
    # Heuristic only: header layout can differ by institution.
    first_page = min(doc.pages, default=1)
    lines = [l for l in doc.lines(first_page) if doc.bbox(l).get('Top', 1) < 0.27]
    lines = [l for l in lines if l.get('Text', '').strip()]
    ignore = ('patient name', 'date of birth', 'prisoner number', 'landing cell', 'in possession', 'not in possession')
    # Print-run stamps ("Printed by Mrs X", "Printed on 12 Oct 2026", "Page 1 of 2") sit above the
    # establishment header and are not the provider.
    stamp = re.compile(r'^(printed (by|on|at)|page \d+ of \d+)\b')
    candidate = [l for l in lines if not any(p in norm(l.get('Text', '')) for p in ignore)
                 and not stamp.match(norm(l.get('Text', '')))]
    name = candidate[0] if candidate else None
    address_lines = candidate[1:4] if name else []
    # Avoid interpreting a patient label as the hospital address.
    address_lines = [l for l in address_lines if not any(match_score(l.get('Text','').split(':')[0], aliases) > .8 for aliases in PATIENT.values())]
    return {'name': _value(name.get('Text') if name else None,
                            doc.provenance([name['Id']]) if name else None),
            'address': _value(' '.join(l.get('Text', '') for l in address_lines),
                               doc.provenance([l['Id'] for l in address_lines]) if address_lines else None)}
