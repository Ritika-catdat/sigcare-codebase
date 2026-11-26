import re
from .handwriting_detector import DATE_TEXT, below_signature, signature_labels
from .medication_table_extractor import HEADER_WORDS
from .output_contract import POSTCODE, STREET
from .schemas import PATIENT, match_score, norm

# Dates on other labels near the signature are not the prescription date.
OTHER_DATE_LABELS = re.compile(r'\b(birth|dob|start|end|stop)\b')


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


# Header cells of a signature table: "Authorising Signature | Print Name | ... | Date".
SIGN_HEADER = re.compile(r'\b(?:signature|signed|sign|authoris\w*|authoriz\w*)\b')
PRESCRIBER_HEADER = re.compile(r'\b(?:authoris\w*|authoriz\w*|prescriber|prescribed)\b')
DATE_HEADER = re.compile(r'^date\b')


def _signature_table_dates(doc):
    """Dates in the "Date" column of tables whose header row has a signature column.

    Some forms put the prescriber's signature and its date in a table rather than under
    a "Signed:" label. Returns (rank, page, top, date, provenance); rank 0 is a table
    headed as the prescriber's ("Authorising Signature"), rank 1 any other signature table.
    """
    found = []
    for table in doc.tables:
        grid = doc.table_grid(table)
        rows = sorted({r for r, _ in grid})
        for r in rows:
            texts = {c: norm(cell.text) for (row, c), cell in grid.items() if row == r and cell.text}
            if not any(SIGN_HEADER.search(t) for t in texts.values()):
                continue
            rank = 0 if any(PRESCRIBER_HEADER.search(t) for t in texts.values()) else 1
            date_cols = [c for c, t in sorted(texts.items()) if DATE_HEADER.match(t)]
            for c in date_cols:
                # The date can share the header cell ("Date: 09 Oct 2026") or sit below it.
                for row in [r] + [x for x in rows if x > r]:
                    cell = grid.get((row, c))
                    found_date = DATE_TEXT.search(cell.text) if cell and cell.text else None
                    if found_date:
                        box = doc.reading_box(table)
                        found.append((rank, table.get('Page', 1), box.get('Top', 0), found_date.group(),
                                      doc.provenance(cell.block_ids, cell.word_ids, cell.page)))
                        break
            if date_cols:
                break
    return sorted(found, key=lambda x: x[:3])


def extract_prescription_date(doc):
    """The date the prescriber signed (printed or handwritten).

    A table headed as the prescriber's signature ("Authorising Signature | ... | Date")
    gives its Date column. Otherwise the prescriber signs straight after the medicine
    tables, so the first "Signed:" label is theirs; the "Medication Received Signatures"
    lines further down have their own (usually empty) dates and are not used. The nearest
    date under the first label is taken, stopping at the next "Signed:" line so an allergy
    start date or the print footer below is never picked up. Then any other signature
    table's Date column, and last the first line starting with "Date".
    """
    table_dates = _signature_table_dates(doc)
    if table_dates and table_dates[0][0] == 0:
        return _value(table_dates[0][3], table_dates[0][4])
    labels = signature_labels(doc)
    if labels:
        label = min(labels, key=lambda l: (l.get('Page', 1), doc.bbox(l).get('Top', 0)))
        lines = sorted((l for l in doc.lines(label.get('Page', 1)) if below_signature(doc, l, [label])),
                       key=lambda l: doc.bbox(l).get('Top', 0))
    else:
        lines = [l for l in doc.lines() if norm(l.get('Text', '')).startswith('date')]
    for line in lines:
        if labels and line is not label and line in labels:
            break
        text = line.get('Text', '')
        found = DATE_TEXT.search(text)
        if found and not OTHER_DATE_LABELS.search(norm(text)):
            return _value(found.group(), doc.provenance([line['Id']]))
    if table_dates:
        return _value(table_dates[0][3], table_dates[0][4])
    return _value(None, None)


# Words that name the establishment: "HMP Lindholme", "HMP/YOI Eastwood Park",
# "Gatwick Immigration Removal Centre". "Prison Number" is a patient label, not a site.
ESTABLISHMENT = (r'hmp|hmyoi|yoi|hm\s+prison|prison(?!\s*(?:number|no)\b)|young\s+offenders?\s+institution'
                 r'|immigration\s+removal\s+centre|irc|secure\s+training\s+centre')
# A prison or centre name outranks a department inside it: "HMP Huntercombe" over
# "Healthcare Centre, Nuffield, ...". It is the site even when it ends in a street word
# ("HMP Fosse Way").
STRONG_SITE = re.compile(rf'\b(?:{ESTABLISHMENT})\b', re.IGNORECASE)
SITE_WORDS = re.compile(
    rf'\b(?:{ESTABLISHMENT}|hospital|healthcare|health\s+centre'
    r'|medical\s+centre|surgery|pharmacy|clinic)\b', re.IGNORECASE)
# Print-run stamps and page footers are never the site or address: "Printed by Mrs X",
# "08/10/2026 13:37 / Printed by Chloe Campbell (Page 1 of 1)", "Page 1 of 2",
# "NHS Confidential: Personal Data about a Patient", "13/10/2026 13:58".
STAMP = re.compile(r'\bprinted\s+(?:by|on|at)\b|\bpage\s+\d+\s+of\s+\d+\b|\bconfidential\b|\bpersonal\s+data\b'
                   r'|\b\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\b', re.IGNORECASE)
CONTACT = re.compile(r'^(tel|telephone|phone|fax|email|e mail|web|www)\b')
PATIENT_TEXT = ('patient name', 'date of birth', 'prisoner number', 'prison number', 'landing cell',
                'in possession', 'not in possession')
# The establishment header sits in the top part of the page (measured on the upright page).
HEADER_TOP = .35
# Lines further apart than this many line heights are not one block of text.
BLOCK_GAP = 3


def _provider_lines(doc):
    """Lines that could belong to the establishment header, in reading order.

    Table text (patient box, medicine tables), print stamps, page footers, patient labels
    and lines made only of table-header words ("quantity", "name") are left out. Pages
    Textract saw sideways or upside down are put back upright first, so a footer is never
    taken for the header.
    """
    in_table = doc.table_word_ids()
    found = []
    for line in doc.lines():
        children = doc.child_words(line)
        # A note written across the header ("Dossett box, scan both sides!") is not part
        # of the address it runs over: only the printed words are kept.
        printed = [w for w in children if w.get('TextType') != 'HANDWRITING']
        if children and not printed:
            continue
        text = (' '.join(w.get('Text', '') for w in printed) if len(printed) < len(children)
                else line.get('Text', '')).strip()
        if text != line.get('Text', ''):
            line = {**line, 'Text': text}
        words = norm(text).split()
        if not words or STAMP.search(text) or set(words) <= HEADER_WORDS:
            continue
        if any(p in ' '.join(words) for p in PATIENT_TEXT):
            continue
        if any(match_score(text.split(':')[0], aliases) > .8 for aliases in PATIENT.values()):
            continue
        if children and 2 * sum(w['Id'] in in_table for w in children) > len(children):
            continue
        found.append(line)
    return sorted(found, key=lambda l: (l.get('Page', 1), round(doc.reading_box(l).get('Top', 0), 3),
                                        doc.reading_box(l).get('Left', 0)))


def _steps(doc, lines, start, direction):
    """Lines of the same block as start, nearest first: above it (-1) or below it (+1).

    Lines beside one another on the same row and lines that do not overlap start
    horizontally (a box at the other side of the page) are skipped.
    """
    box = doc.reading_box(start)
    index = lines.index(start)
    others = reversed(lines[:index]) if direction < 0 else lines[index + 1:]
    current = box
    for line in others:
        b = doc.reading_box(line)
        if line.get('Page', 1) != start.get('Page', 1):
            return
        height = max(current.get('Height', 0), b.get('Height', 0)) or .02
        distance = abs(b.get('Top', 0) - current.get('Top', 0))
        if distance <= .3 * height:
            continue
        if distance > BLOCK_GAP * height:
            return
        overlap = (min(box.get('Left', 0) + box.get('Width', 0), b.get('Left', 0) + b.get('Width', 0))
                   - max(box.get('Left', 0), b.get('Left', 0)))
        if overlap <= 0:
            continue
        current = b
        yield line


def _split_site(text, pattern=SITE_WORDS):
    """('HMP/YOI Eastwood Park', ['Wotton-under-Edge', 'Gloucestershire', 'GL12 8DB']) from
    'Wotton-under-Edge, Gloucestershire, GL12 8DB, HMP/YOI Eastwood Park'; (None, parts)
    when no part names an establishment. A street ("Hospital Lane") is not a site, but a
    prison named after one ("HMP Fosse Way") is."""
    parts = [p.strip() for p in text.split(',') if p.strip()]
    for i, part in enumerate(parts):
        if pattern.search(part) and (STRONG_SITE.search(part) or not STREET.search(part)):
            return parts.pop(i), parts
    return None, parts


def _join(texts, site=None):
    """A wrapped address keeps its own commas; separate lines become comma-separated parts.
    A part repeated (a header printing the site on its own line and again in the address
    line) and the site name itself are kept once / left out."""
    address = ''
    for text in texts:
        text = text.strip().strip(',').strip() if not address else text.strip()
        if text:
            address = f'{address} {text}' if address.endswith(',') or not address else f'{address}, {text}'
    parts, seen = [], {norm(site)} if site else set()
    for part in (p.strip() for p in address.split(',')):
        if part and norm(part) not in seen:
            seen.add(norm(part))
            parts.append(part)
    return ', '.join(parts)


def extract_provider(doc):
    """Site name and address from the establishment header, whatever its layout.

    The address is found by its UK postcode, so it does not matter where the header sits,
    whether print stamps or the patient's name share its row, or whether the page was
    scanned upside down. The site name is the part naming the establishment (HMP, YOI,
    Immigration Removal Centre, ...): on the postcode line itself ("..., GL12 8DB, HMP/YOI
    Eastwood Park"), or the line above it ("HMP Lindholme" over "Bawtry Road, Doncaster,
    ..."). Address lines between the two are kept. Without a postcode the site line is
    found by those words and the address is the lines under it.
    """
    lines = _provider_lines(doc)
    header = [l for l in lines if doc.reading_box(l).get('Top', 1) < HEADER_TOP]
    # A prison/centre name first; a department or surgery name only when there is none.
    site, site_ids, texts, address_ids = _find_provider(doc, lines, header, STRONG_SITE)
    if site is None:
        site, site_ids, texts, address_ids = _find_provider(doc, lines, header, SITE_WORDS)
    address = _join(texts, site)
    return {'name': _value(site, doc.provenance(site_ids) if site else None),
            'address': _value(address, doc.provenance(address_ids) if address else None)}


def _find_provider(doc, lines, header, site_words):
    def first(pattern):
        # The header area of the earliest page first, then anywhere outside the tables.
        return next((l for group in (header, lines) for l in group if pattern.search(l.get('Text', ''))), None)

    site, site_ids, texts, address_ids = None, [], [], []
    anchor = first(POSTCODE)
    if anchor is not None:
        site, parts = _split_site(anchor['Text'], site_words)
        site_ids = [anchor['Id']] if site else []
        texts, address_ids = [', '.join(parts)], [anchor['Id']]
        for line in _steps(doc, lines, anchor, -1):
            text = line['Text'].strip()
            if CONTACT.match(norm(text)):
                break
            line_site, line_parts = _split_site(text, site_words)
            if line_site or (site is None and not _looks_like_address(text)):
                if site is None:
                    site, site_ids = line_site or text, [line['Id']]
                    if line_site and line_parts:
                        texts.insert(0, ', '.join(line_parts))
                        address_ids.insert(0, line['Id'])
                break
            if len(address_ids) > 3:
                break
            texts.insert(0, text)
            address_ids.insert(0, line['Id'])
        if site is None:
            below = next(_steps(doc, lines, anchor, 1), None)
            if below is not None and site_words.search(below['Text']):
                site, site_ids = below['Text'].strip(), [below['Id']]
    elif site_words is SITE_WORDS or first(site_words) is not None:
        start = first(site_words)
        if start is None:
            first_page = min(doc.pages, default=1)
            start = next((l for l in header if l.get('Page', 1) == first_page), None)
        if start is not None:
            site, parts = _split_site(start['Text'], site_words)
            if site is None:
                site, parts = start['Text'].strip(), []
            site_ids = [start['Id']]
            if parts:
                texts, address_ids = [', '.join(parts)], [start['Id']]
            for line in _steps(doc, lines, start, 1):
                text = line['Text'].strip()
                if len(texts) >= 3 or CONTACT.match(norm(text)) or STRONG_SITE.search(text):
                    break
                texts.append(text)
                address_ids.append(line['Id'])
    return site, site_ids, texts, address_ids


def _looks_like_address(text):
    """An address line has commas, a house number or a street word."""
    return ',' in text or any(ch.isdigit() for ch in text) or bool(STREET.search(text))
