"""Exact downstream prescription JSON contract, separate from audit-rich extraction."""
import re
from pathlib import PurePosixPath

IN_FIELDS = ('drug_name','dose','script_notes','start_date','days_duration','quantity','medication_type','possession_quantity','possession_interval','medication_notes','prescriber_name','prescriber')
NOT_FIELDS = ('drug_name','administration_dose_time_description','start_date','end_date','days_duration','quantity','issuer','medication_notes','prescriber_name','prescriber')
ALLERGY_FIELDS = ('start_date','end_date','allergy_or_sensitivity')


def _str(value):
    return '' if value is None else str(value)


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
    ids = set(handwriting.get('top_handwriting_word_ids', []))
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
    status = internal['extraction_metadata']['status']
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
    flag_for_review = status != 'EXTRACTED'
    return {
        'patient_name': values['name'],
        'date_of_birth': values['date_of_birth'],
        'age': values['age'],
        'prisoner_number': values['prisoner_number'],
        'wing': values['wing'],
        'Landing': landing,
        'cell': cell,
        'identification_ocr_confidence': _avg(confidence),
        'in_possession': ip,
        'in_possession_record_count': len(ip),
        'not_in_possession': nip,
        'not_in_possession_record_count': len(nip),
        'allergy': allergy,
        'allergy_record_count': len(allergy),
        'hand_written_notes_present': bool(notes) or handwriting.get('handwritten_notes', False),
        'hand_written_notes': notes,
        'notes_records_count': len(notes),
        'flag_for_review': flag_for_review,
        # Reasons are only reported when the prescription needs review.
        'review_reasons': list(internal['extraction_metadata'].get('review_reasons', [])) if flag_for_review else [],
        'overall_ocr_confidence': overall,
        'file_name': PurePosixPath(source['key']).name,
        'prescription_number': prescription_number,
        'original_pdf_location': original_pdf_location,
        'prescription_location': prescription_location,
    }


def failed_output(source, reasons):
    """Same contract for a prescription whose extraction could not run at all."""
    return {
        'patient_name': '', 'date_of_birth': '', 'age': '', 'prisoner_number': '', 'wing': '',
        'Landing': '', 'cell': '', 'identification_ocr_confidence': '',
        'in_possession': [], 'in_possession_record_count': 0,
        'not_in_possession': [], 'not_in_possession_record_count': 0,
        'allergy': [], 'allergy_record_count': 0,
        'hand_written_notes_present': False, 'hand_written_notes': [], 'notes_records_count': 0,
        'flag_for_review': True,
        'review_reasons': list(reasons),
        'overall_ocr_confidence': '',
        'file_name': PurePosixPath(source['key']).name,
        'prescription_number': _str(source.get('prescription_number')),
        'original_pdf_location': _str(source.get('original_pdf_location')),
        'prescription_location': _str(source.get('prescription_location')),
    }
