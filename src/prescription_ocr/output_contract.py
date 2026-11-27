"""Exact downstream prescription JSON contract, separate from audit-rich extraction."""
import re
from datetime import datetime
from pathlib import PurePosixPath

IN_FIELDS = ('drug_name','dose','script_notes','start_date','days_duration','quantity','medication_type','possession_quantity','possession_interval','medication_notes','prescriber_name','prescriber')
NOT_FIELDS = ('drug_name','dose','administration_dose_time_description','start_date','end_date','days_duration','quantity','issuer','medication_notes','prescriber_name','prescriber')
ALLERGY_FIELDS = ('start_date','end_date','allergy_or_sensitivity')


def _str(value):
    return '' if value is None else str(value)


SALUTATIONS = {'mr', 'mrs', 'ms', 'miss', 'mx', 'master', 'dr', 'prof', 'professor',
               'sir', 'dame', 'lady', 'lord', 'rev', 'revd', 'fr', 'sr'}
DATE_FORMATS = ('%d %b %Y', '%d %B %Y', '%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y', '%Y-%m-%d',
                '%d-%b-%Y', '%d %b %y', '%d/%m/%y', '%d-%m-%y')


def split_name(name):
    """Salutation, first, middle and last name from "Mr First Middle Last" or "Last, Mr First Middle"."""
    def is_salutation(word):
        return word.rstrip('.').casefold() in SALUTATIONS

    out = {'salutation': '', 'first_name': '', 'middle_name': '', 'last_name': ''}
    name = ' '.join(_str(name).split())
    if ',' in name:
        last, rest = name.split(',', 1)
        out['last_name'] = last.strip()
        words = rest.split()
    else:
        words = name.split()
        if len(words) > 1 and not (len(words) == 2 and is_salutation(words[0])):
            out['last_name'] = words.pop()
    if words and is_salutation(words[0]):
        out['salutation'] = words.pop(0)
    if words:
        out['first_name'] = words.pop(0)
    out['middle_name'] = ' '.join(words)
    return out


def transform_date(value):
    """'26 Mar 1995' -> '1995-03-26' (UK day-first); '' when the text is not a date."""
    text = re.sub(r'(\d)(st|nd|rd|th)\b', r'\1', _str(value).strip(), flags=re.IGNORECASE)
    text = re.sub(r'\bsept\b', 'Sep', re.sub(r'\s+', ' ', text), flags=re.IGNORECASE).replace(',', '')
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return ''


# A day-month-year date inside other text: "19 Dec 1990", "19/12/1990", "1st Sept 1990", "1990-12-19".
DATE_IN_TEXT = re.compile(
    r'\b\d{1,2}(?:st|nd|rd|th)?[\s/.-]+(?:\d{1,2}|[A-Za-z]{3,9}\.?)[\s/.-]+\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b',
    re.IGNORECASE)


def date_only(value):
    """'19 Dec 1990 (35 y)' -> '19 Dec 1990': only the day, month and year are kept, without the
    age or any other text beside them. Text with no readable date is returned unchanged so a
    reviewer can still see it."""
    text = ' '.join(_str(value).split())
    for match in DATE_IN_TEXT.finditer(text):
        date = match.group().rstrip('.')
        if transform_date(date):
            return date
    return text


# UK postcode, allowing OCR reading 0 as O in the inward part ("BN18 OBX").
TRAILING_PRISONER_NUMBER = re.compile(r'[\s,;/-]+[A-Z]\d{4}[A-Z]{2}$')
POSTCODE = re.compile(r'\b[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9O][A-Z]{2}\b', re.IGNORECASE)
# A phone/reference number after a separator: "/ 01293 566549", "\ Tel: +44 (0)1293 566549".
TRAILING_NUMBER = re.compile(
    r'\s*[/\|,;:\u2013\u2014-]+\s*(?:(?:tel|telephone|phone|fax|t)\.?\s*:?\s*)?[+()\d][+()\d\s.-]*$',
    re.IGNORECASE)


def clean_address(address):
    """Keep the address up to the postcode; drop a phone or long number after it."""
    text = ' '.join(_str(address).split())
    matches = list(POSTCODE.finditer(text))
    if matches:
        return text[:matches[-1].end()].strip()
    # No recognisable postcode: remove a trailing number of 6+ digits after a separator.
    match = TRAILING_NUMBER.search(text)
    if match and sum(ch.isdigit() for ch in match.group()) >= 6:
        text = text[:match.start()]
    return text.strip(' ,/\|;:-')


# A street or house-number part of an address is never the city.
# Short forms count only at the end ("High St"), so "St Albans" stays a town.
STREET = re.compile(
    r'^\s*\d|\b(?:road|street|lane|avenue|drive|way|close|crescent|place|terrace|grove'
    r'|court|square|row|walk|parade|gardens|mews|boulevard|hill)\b|\b(?:rd|st|ln|ave|dr)\.?\s*$', re.IGNORECASE)
# UK counties (ceremonial, historic and the usual short forms), compared lower case.
COUNTIES = {
    'bedfordshire', 'berkshire', 'bristol', 'buckinghamshire', 'cambridgeshire', 'cheshire', 'cornwall',
    'cumbria', 'cumberland', 'derbyshire', 'devon', 'dorset', 'durham', 'county durham', 'east riding of yorkshire',
    'east sussex', 'essex', 'gloucestershire', 'greater london', 'greater manchester', 'hampshire',
    'herefordshire', 'hertfordshire', 'isle of wight', 'kent', 'lancashire', 'leicestershire', 'lincolnshire',
    'london', 'merseyside', 'middlesex', 'norfolk', 'north yorkshire', 'northamptonshire', 'northumberland',
    'nottinghamshire', 'oxfordshire', 'rutland', 'shropshire', 'somerset', 'south yorkshire', 'staffordshire',
    'suffolk', 'surrey', 'sussex', 'tyne and wear', 'warwickshire', 'west midlands', 'west sussex',
    'west yorkshire', 'wiltshire', 'worcestershire', 'yorkshire', 'east yorkshire', 'avon', 'cleveland',
    'humberside', 'westmorland',
    'beds', 'berks', 'bucks', 'cambs', 'glos', 'gloucs', 'hants', 'herts', 'lancs', 'leics', 'lincs',
    'northants', 'notts', 'oxon', 'salop', 'staffs', 'warks', 'wilts', 'worcs', 'yorks',
    'anglesey', 'cardiff', 'carmarthenshire', 'ceredigion', 'clwyd', 'conwy', 'denbighshire', 'dyfed',
    'flintshire', 'glamorgan', 'gwent', 'gwynedd', 'monmouthshire', 'pembrokeshire', 'powys', 'swansea',
    'mid glamorgan', 'south glamorgan', 'west glamorgan',
    'aberdeenshire', 'angus', 'argyll', 'ayrshire', 'dumfries and galloway', 'fife', 'highland',
    'lanarkshire', 'lothian', 'midlothian', 'east lothian', 'west lothian', 'perth and kinross',
    'renfrewshire', 'scottish borders', 'stirlingshire', 'dunbartonshire',
    'antrim', 'armagh', 'down', 'fermanagh', 'londonderry', 'tyrone',
}
COMPASS = {'n': 'north', 's': 'south', 'e': 'east', 'w': 'west'}


def is_county(part):
    """'South Yorkshire', 'W Sussex', 'Co. Durham', 'Gloucestershire' -> True."""
    words = re.sub(r'[^a-z ]+', ' ', _str(part).casefold()).split()
    if words and words[0] in ('co', 'county') and len(words) > 1:
        words = words[1:]
    if words and words[0] in COMPASS:
        words[0] = COMPASS[words[0]]
    return ' '.join(words) in COUNTIES or ' '.join(words[1:]) in COUNTIES and words[0] in COMPASS.values()


def split_address(site_name, address):
    """Site name is line 1. In the address the postcode is the UK postcode at the end, the
    county the part before it when it names a county (or, in a long address, the second-last
    part), the city the part before that, line 2 the first part and line 3 whatever is left.
    A street or house number is never taken as the city."""
    parts = [p.strip() for p in _str(address).split(',') if p.strip()]
    out = {'address_line_1': _str(site_name), 'address_line_2': '', 'address_line_3': '',
           'city': '', 'county': '', 'postcode': ''}
    found = list(POSTCODE.finditer(parts[-1])) if parts else []
    if found:
        # "Doncaster DN7 6EE" without a comma: the rest of the part stays in the address.
        out['postcode'] = found[-1].group()
        rest = parts.pop()[:found[-1].start()].strip(' ,')
        if rest:
            parts.append(rest)
    if parts and (is_county(parts[-1]) or len(parts) >= 4):
        out['county'] = parts.pop()
    if len(parts) >= 2 or (len(parts) == 1 and (out['county'] or out['postcode'])
                           and not STREET.search(parts[0])):
        out['city'] = parts.pop()
    if parts:
        out['address_line_2'] = parts.pop(0)
    out['address_line_3'] = ', '.join(parts)
    return out


def _avg(values):
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return round(sum(nums) / len(nums), 2) if nums else ''


def _provenance_confidence(prov):
    if not isinstance(prov, dict):
        return None
    if 'components' in prov:
        return _avg([_provenance_confidence(p) for p in prov['components']]) or None
    return prov.get('ocr_confidence')


def _patient_field(patient, name):
    field = patient.get(name) or {}
    return _str(field.get('value')), _provenance_confidence(field.get('provenance'))


def _with_handwriting(value, words):
    """Show a handwritten value apart from the printed text of its cell.

    "28 day(s) 7" with a handwritten "7" becomes "28 day(s) [handwritten: 7]", so the
    reviewer sees both the printed value and the hand correction. A cell written
    entirely by hand keeps its text as it is.
    """
    if not words or not value:
        return value
    tokens = value.split()
    for word in words:
        for i in range(len(tokens) - 1, -1, -1):
            if tokens[i] == word:
                del tokens[i]
                break
    printed = ' '.join(tokens)
    return f'{printed} [handwritten: {" ".join(words)}]' if printed else value


def _record(row, fields):
    # Keep distinct prescriber_name and prescriber in the externally specified schema;
    # when only one printed header exists, do NOT hallucinate the other value.
    provenance = row.get('_provenance') or {}
    handwritten = row.get('_handwritten') or {}
    out = {}
    for name in fields:
        out[name] = _with_handwriting(_str(row.get(name)), handwritten.get(name))
        if name == 'script_notes':
            # Script notes are checked on their own confidence, not only the record average.
            out['script_notes_ocr_confidence'] = (
                _avg([_provenance_confidence(provenance.get(name))]) if out[name].strip() else '')
    confidences = [_provenance_confidence(provenance.get(k)) for k in fields if row.get(k) not in (None, '')]
    out['record_ocr_confidence'] = _avg(confidences)
    return out


def _handwritten_notes(doc, handwriting):
    ids = set(handwriting.get('note_word_ids', []))
    if not ids:
        return []
    # Group handwritten words by the Textract LINE parent; do not fragment notes into words.
    seen = set()
    notes = []
    for line in doc.lines():
        words = [w for w in doc.child_words(line) if w.get('Id') in ids]
        if not words:
            continue
        text = ' '.join(w.get('Text', '') for w in words).strip()
        if text:
            notes.append({'note': text, 'note_ocr_confidence': _avg([w.get('Confidence') for w in words])})
            seen.update(w.get('Id') for w in words)
    # Handle orphan words not associated with a LINE.
    for ident in sorted(ids - seen):
        word = doc.index.get(ident)
        if word and word.get('Text'):
            notes.append({'note': word['Text'], 'note_ocr_confidence': _avg([word.get('Confidence')])})
    return notes



def _review_reasons(values, notes_present, overall, in_possession, not_in_possession, prior_reasons, identification_confidence=100.0):
    """Compute all independent review triggers. OCR confidence is stored on 0..100 scale."""
    reasons = []
    if notes_present:
        reasons.append('Handwritten notes present')
    for field, label in (('name','patient_name'), ('date_of_birth','date_of_birth'),
                         ('prisoner_number','prisoner_number')):
        if not str(values.get(field) or '').strip():
            reasons.append(f'Missing required patient field: {label}')
    # 0.9 normalized threshold corresponds to 90.0 on Textract's 0–100 scale.
    if not isinstance(overall, (int, float)):
        reasons.append('Overall OCR confidence unavailable')
    elif overall < 90.0:
        reasons.append(f'Overall OCR confidence below 0.9: {overall / 100:.4f}')
    if not isinstance(identification_confidence, (int, float)):
        reasons.append('Identification OCR confidence unavailable')
    elif identification_confidence < 90.0:
        reasons.append(f'Identification OCR confidence below 0.9: {identification_confidence / 100:.4f}')
    for section, records in (('in_possession', in_possession), ('not_in_possession', not_in_possession)):
        for index, record in enumerate(records, start=1):
            conf = record.get('record_ocr_confidence') if isinstance(record, dict) else None
            if not isinstance(conf, (int, float)):
                reasons.append(f'{section} record {index}: OCR confidence unavailable')
            elif conf < 90.0:
                reasons.append(f'{section} record {index}: OCR confidence below 0.9: {conf / 100:.4f}')
            if not isinstance(record, dict):
                continue
            if _str(record.get('medication_notes')).strip():
                reasons.append(f'{section} record {index}: medication notes present')
            if _str(record.get('script_notes')).strip():
                conf = record.get('script_notes_ocr_confidence')
                if not isinstance(conf, (int, float)):
                    reasons.append(f'{section} record {index}: script notes OCR confidence unavailable')
                elif conf < 90.0:
                    reasons.append(f'{section} record {index}: script notes OCR confidence below 0.9: {conf / 100:.4f}')
    if not in_possession and not not_in_possession:
        reasons.append('Neither in_possession nor not_in_possession contains a record')
    # Retain upstream detected issues, e.g. handwritten tables or failed table classification.
    for reason in prior_reasons or []:
        if reason not in reasons:
            reasons.append(reason)
    return reasons

def build_output(internal, doc, handwriting, source, output_bucket, output_key):
    patient = internal['patient']
    # Age, wing, landing and cell are not part of the output (their labels are still
    # recognised during extraction, so they are never taken as another field's value).
    identity_fields = ('name','date_of_birth','prisoner_number')
    values, confidence = {}, []
    for name in identity_fields:
        value, conf = _patient_field(patient, name)
        values[name] = value
        if value and conf is not None:
            confidence.append(conf)
    # The cell can hold more than the date ("19 Dec 1990 (35 y)"); only the date is kept.
    values['date_of_birth'] = date_only(values['date_of_birth'])
    # "Patient name: Mr Richard Osborne, A5595AL" prints the prisoner number after the name.
    values['name'] = TRAILING_PRISONER_NUMBER.sub('', values['name']).strip()
    prescription_date = date_only((internal.get('prescription_date') or {}).get('value'))
    ip = [_record(r, IN_FIELDS) for r in internal.get('in_possession_medicines', [])]
    nip = [_record(r, NOT_FIELDS) for r in internal.get('not_in_possession_medicines', [])]
    allergy = [_record(r, ALLERGY_FIELDS) for r in internal.get('allergies', [])]
    notes = _handwritten_notes(doc, handwriting)
    # Count all OCR WORD blocks exactly once (not table and field averages).
    overall = _avg([b.get('Confidence') for b in doc.blocks if b.get('BlockType')=='WORD'])
    metadata = internal.get('extraction_metadata') or {}
    notes_present = bool(notes) or bool(handwriting.get('handwritten_notes', False))
    identification_confidence = _avg(confidence)
    review_reasons = _review_reasons(values, notes_present, overall, ip, nip, metadata.get('review_reasons'), identification_confidence)
    # TODO (preprocessing integration): provide the S3 URI of the ORIGINAL,
    # processed PDF from which the individual prescription PDF was split.
    # Set source['original_pdf_location'] from your preprocessing manifest.
    original_pdf_location = _str(source.get('original_pdf_location'))

    # TODO (preprocessing integration): provide the prescription's ORDINAL NUMBER
    # relative to ALL prescriptions in the original source PDF (e.g. "2" for
    # the second prescription). Do not use the generated OCR document ID here.
    prescription_number = _str(source.get('prescription_number'))

    # TODO (preprocessing integration): supply the S3 URI of the SPLIT PDF
    # actually being OCR-extracted. By default the input S3 object is that file.
    prescription_location = _str(source.get('prescription_location')) or f"s3://{source['bucket']}/{source['key']}"
    provider = internal.get('provider') or {}
    site_name = _str((provider.get('name') or {}).get('value'))
    address = clean_address((provider.get('address') or {}).get('value'))
    return {
        'site_name': site_name,
        'address': address,
        **split_address(site_name, address),
        'patient_name': values['name'],
        **split_name(values['name']),
        'date_of_birth': values['date_of_birth'],
        'date_of_birth_transformed': transform_date(values['date_of_birth']),
        'prisoner_number': values['prisoner_number'],
        'identification_ocr_confidence': identification_confidence,
        'prescription_date': prescription_date,
        'prescription_date_transformed': transform_date(prescription_date),
        'in_possession': ip,
        'in_possession_record_count': len(ip),
        'not_in_possession': nip,
        'not_in_possession_record_count': len(nip),
        'allergy': allergy,
        'allergy_record_count': len(allergy),
        'hand_written_notes_present': notes_present,
        'hand_written_notes': notes,
        'notes_records_count': len(notes),
        'flag_for_review': bool(review_reasons) or metadata.get('status') != 'EXTRACTED',
        'review_reason': review_reasons,
        'overall_ocr_confidence': overall,
        'file_name': PurePosixPath(source['key']).name,
        'prescription_number': prescription_number,
        'original_pdf_location': original_pdf_location,
        'prescription_location': prescription_location,
    }


def failed_output(source, reasons):
    """Same contract for a prescription whose extraction could not run at all."""
    return {
        'site_name': '', 'address': '', **split_address('', ''),
        'patient_name': '', **split_name(''), 'date_of_birth': '', 'date_of_birth_transformed': '', 'prisoner_number': '',
        'identification_ocr_confidence': '', 'prescription_date': '', 'prescription_date_transformed': '',
        'in_possession': [], 'in_possession_record_count': 0,
        'not_in_possession': [], 'not_in_possession_record_count': 0,
        'allergy': [], 'allergy_record_count': 0,
        'hand_written_notes_present': False, 'hand_written_notes': [], 'notes_records_count': 0,
        'flag_for_review': True,
        'review_reason': list(reasons),
        'overall_ocr_confidence': '',
        'file_name': PurePosixPath(source['key']).name,
        'prescription_number': _str(source.get('prescription_number')),
        'original_pdf_location': _str(source.get('original_pdf_location')),
        'prescription_location': _str(source.get('prescription_location')),
    }
