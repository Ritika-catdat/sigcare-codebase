"""Exact downstream prescription JSON contract, separate from audit-rich extraction."""
import re
from datetime import datetime
from pathlib import PurePosixPath

IN_FIELDS = ('drug_name','dose','script_notes','start_date','days_duration','quantity','medication_type','possession_quantity','possession_interval','medication_notes','prescriber_name','prescriber')
NOT_FIELDS = ('drug_name','administration_dose_time_description','start_date','end_date','days_duration','quantity','issuer','medication_notes','prescriber_name','prescriber')
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


def split_address(site_name, address):
    """Site name is line 1. In the address, after the last comma is the postcode, between the
    last two commas the county, between the third- and second-last the city, up to the first
    comma line 2, and whatever is left line 3."""
    parts = [p.strip() for p in _str(address).split(',') if p.strip()]
    out = {'address_line_1': _str(site_name), 'address_line_2': '', 'address_line_3': '',
           'city': '', 'county': '', 'postcode': ''}
    for field in ('postcode', 'county', 'city'):
        if len(parts) >= 2:
            out[field] = parts.pop()
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


def _landing_and_cell(patient):
    landing, _ = _patient_field(patient, 'landing')
    cell, _ = _patient_field(patient, 'cell')
    combined, _ = _patient_field(patient, 'landing_cell')
    if combined and (not landing or not cell):
        parts = re.split(r'\s*/\s*|\s*\|\s*', combined, maxsplit=1)
        if len(parts) == 2:
            landing = landing or parts[0]
            cell = cell or parts[1]
        elif not landing:
            landing = combined
    return landing, cell


def _record(row, fields):
    out = {name: _str(row.get(name)) for name in fields}
    # Keep distinct prescriber_name and prescriber in the externally specified schema;
    # when only one printed header exists, do NOT hallucinate the other value.
    provenance = row.get('_provenance') or {}
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
                         ('age','age'), ('prisoner_number','prisoner_number')):
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
    if not in_possession and not not_in_possession:
        reasons.append('Neither in_possession nor not_in_possession contains a record')
    # Retain upstream detected issues, e.g. handwritten tables or failed table classification.
    for reason in prior_reasons or []:
        if reason not in reasons:
            reasons.append(reason)
    return reasons

def build_output(internal, doc, handwriting, source, output_bucket, output_key):
    patient = internal['patient']
    identity_fields = ('name','date_of_birth','age','prisoner_number','wing','landing','cell')
    values, confidence = {}, []
    for name in identity_fields:
        value, conf = _patient_field(patient, name)
        values[name] = value
        if value and conf is not None:
            confidence.append(conf)
    landing, cell = _landing_and_cell(patient)
    values['landing'], values['cell'] = landing, cell
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
    address = _str((provider.get('address') or {}).get('value'))
    return {
        'site_name': site_name,
        'address': address,
        **split_address(site_name, address),
        'patient_name': values['name'],
        **split_name(values['name']),
        'date_of_birth': values['date_of_birth'],
        'date_of_birth_transformed': transform_date(values['date_of_birth']),
        'age': values['age'],
        'prisoner_number': values['prisoner_number'],
        'wing': values['wing'],
        'Landing': landing,
        'cell': cell,
        'identification_ocr_confidence': identification_confidence,
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
        'patient_name': '', **split_name(''), 'date_of_birth': '', 'date_of_birth_transformed': '', 'age': '', 'prisoner_number': '', 'wing': '',
        'Landing': '', 'cell': '', 'identification_ocr_confidence': '',
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
