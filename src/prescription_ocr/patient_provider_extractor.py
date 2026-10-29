import re
from .schemas import PATIENT, match_score, norm


def _value(value, provenance):
    return {'value': value.strip() if isinstance(value, str) and value.strip() else None,
            'provenance': provenance}


# Fields whose value normally has a number in it ("36 y", "06 Jul 1990", "A4094AG").
# A value without one ("y") is often only part of the real value, so a value with a
# number is preferred; a letters-only value is still kept when nothing better exists.
NEEDS_DIGIT = {'age', 'date_of_birth', 'prisoner_number'}


def _valid(field, value):
    return field not in NEEDS_DIGIT or any(ch.isdigit() for ch in value)


def _text_right_of_label(doc, aliases):
    """Words on the same row to the right of a printed label, e.g. "36 y" after "Patient age:".

    Used when the table/form value is incomplete because OCR put part of it in another cell.
    """
    for line in doc.lines():
        label = line.get('Text', '').split(':')[0]
        if match_score(label, aliases) < .9:
            continue
        lb = doc.bbox(line)
        top, height = lb.get('Top', 0), lb.get('Height', 0)
        right = lb.get('Left', 0) + lb.get('Width', 0)
        own = {w['Id'] for w in doc.child_words(line)}
        words = []
        for w in doc.blocks:
            if w.get('BlockType') != 'WORD' or w['Id'] in own or w.get('Page', 1) != line.get('Page', 1):
                continue
            b = doc.bbox(w)
            middle = b.get('Top', 0) + b.get('Height', 0) / 2
            if top - height * .3 <= middle <= top + height * 1.3 and right - .01 <= b.get('Left', 0) <= right + .4:
                words.append(w)
        if words:
            words.sort(key=lambda w: doc.bbox(w).get('Left', 0))
            text = ' '.join(w.get('Text', '') for w in words).lstrip(':').strip()
            return text, doc.provenance([w['Id'] for w in words])
    return None, None


def _is_label(text):
    """'Cell:' or 'Date of birth' on its own is the next field's label, never a value."""
    label, _, rest = text.partition(':')
    return not rest.strip() and any(match_score(label, aliases) >= .9 for aliases in PATIENT.values())


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
            if below and below.text and not _is_label(below.text) and any(match_score(cell.text, aliases) >= .9 for aliases in PATIENT.values()):
                candidates.append((cell.text, below.text, doc.provenance(below.block_ids, below.word_ids, below.page), 1))
    for field, aliases in PATIENT.items():
        # Values keep all their letters and digits ("36 y", "A4094AG"); a value that has a
        # number is only preferred over one that does not.
        ranked = sorted(((_valid(field, value), match_score(key, aliases), priority, value, prov)
                         for key, value, prov, priority in candidates
                         if value.strip() and not _is_label(value)),
                        key=lambda x: (x[0], x[1], x[2]), reverse=True)
        ranked = [r for r in ranked if r[1] >= 0.76]
        best = ranked[0] if ranked else None
        if best and not best[0]:
            # Only a letters-only value was found ("y"): the number may have landed in
            # another cell, so read the whole row beside the label ("36 y") when it has one.
            value, prov = _text_right_of_label(doc, aliases)
            if value and _valid(field, value):
                best = (True, 1.0, 0, value, prov)
        elif not best and field in NEEDS_DIGIT:
            value, prov = _text_right_of_label(doc, aliases)
            if value:
                best = (_valid(field, value), 1.0, 0, value, prov)
        if best:
            output[field] = _value(best[3], best[4])
    # Landing and Cell are often separate labels in the patient header.
    # Combine them when both are present, without overwriting an explicit
    # "Landing / Cell" value already identified above.
    def best(label):
        ranked = sorted(((match_score(key, [label]), value, prov)
                         for key, value, prov, priority in candidates if value.strip() and not _is_label(value)),
                        key=lambda x: x[0], reverse=True)
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
    address_lines = []
    # The address is the lines under the site name, up to the contact details.
    contact = re.compile(r'^(tel|telephone|phone|fax|email|e mail|web|www)\b')
    for l in (candidate[1:4] if name else []):
        text = l.get('Text', '')
        if contact.match(norm(text)):
            break
        # Avoid interpreting a patient label as the hospital address.
        if not any(match_score(text.split(':')[0], aliases) > .8 for aliases in PATIENT.values()):
            address_lines.append(l)
    # A wrapped address keeps its own commas; separate lines become comma-separated parts.
    address = ''
    for l in address_lines:
        text = l.get('Text', '').strip()
        address = f'{address} {text}' if address.endswith(',') or not address else f'{address}, {text}'
    return {'name': _value(name.get('Text') if name else None,
                            doc.provenance([name['Id']]) if name else None),
            'address': _value(address.strip(),
                               doc.provenance([l['Id'] for l in address_lines]) if address_lines else None)}
