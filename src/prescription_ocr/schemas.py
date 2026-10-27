import re
from difflib import SequenceMatcher

PATIENT = {
    'name': ['patient name', 'name of patient', 'patient'],
    'date_of_birth': ['date of birth', 'dob', 'birth date'],
    'age': ['patient age', 'age'],
    'prisoner_number': ['prisoner number', 'prison number', 'prisoner no', 'prison no'],
    'wing': ['wing'],
    'landing_cell': ['landing cell', 'landing / cell', 'landing/cell', 'landing', 'cell', 'landing location']
}
TABLES = {
    'in_possession_medicines': {
        'drug_name': ['drug name', 'medicine name', 'medication name', 'drug'],
        'dose': ['dose', 'dosage'],
        'script_notes': ['script notes', 'script note'],
        'start_date': ['start date', 'commencement date'],
        'days_duration': ['days duration', 'duration days', 'duration'],
        'quantity': ['quantity', 'qty'],
        'medication_type': ['medication type', 'medicine type'],
        'possession_quantity': ['possession quantity', 'in possession quantity', 'ip quantity'],
        'possession_interval': ['possession interval', 'in possession interval', 'ip interval'],
        'medication_notes': ['medication notes', 'medicine notes', 'notes'],
        'prescriber_name': ['prescriber name'],
        'prescriber': ['prescriber']
    },
    'not_in_possession_medicines': {
        'drug_name': ['drug name', 'medicine name', 'medication name', 'drug'],
        'administration_dose_time_description': ['administration dose time description', 'administration dose / time description', 'administration dose', 'administration time description', 'dose time description'],
        'start_date': ['start date'],
        'end_date': ['end date', 'stop date'],
        'days_duration': ['days duration', 'duration days', 'duration'],
        'quantity': ['quantity', 'qty'],
        'medication_notes': ['medication notes', 'medicine notes', 'notes'],
        'prescriber_name': ['prescriber name'],
        'prescriber': ['prescriber'],
        'issuer': ['issuer', 'issued by', 'issuing clinician']
    },
    'allergies': {
        'start_date': ['start date', 'date'],
        'allergy_or_sensitivity': ['allergy or sensitivity', 'allergy sensitivity', 'allergy', 'sensitivity'],
        'end_date': ['end date', 'stop date', 'date ended']
    }
}
CRITICAL_FIELDS = {'in_possession_medicines': {'drug_name', 'dose'},
                   'not_in_possession_medicines': {'drug_name', 'administration_dose_time_description'},
                   'allergies': {'allergy_or_sensitivity'}}

def norm(s):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(s).casefold())).strip()

def match_score(actual, labels):
    a = norm(actual)
    if not a:
        return 0.0
    scores = []
    for label in labels:
        b = norm(label)
        if a == b:
            return 1.0
        if len(a) >= 5 and len(b) >= 5 and (a in b or b in a):
            scores.append(0.88)
        scores.append(SequenceMatcher(None, a, b).ratio() * 0.82)
    return max(scores, default=0.0)

def map_headers(headers, schema):
    """One-to-one greedy header assignment; avoids mapping 'quantity' to possession quantity."""
    candidates = sorted(((match_score(h, aliases), idx, field)
                         for idx, h in enumerate(headers) for field, aliases in schema.items()), reverse=True)
    mapping, used = {}, set()
    for score, idx, field in candidates:
        if score < 0.72 or idx in used or field in mapping:
            continue
        mapping[field] = idx
        used.add(idx)
    return mapping

def classify(headers, title='', context=''):
    scored = []
    context = norm(context + ' ' + title)
    for kind, schema in TABLES.items():
        mapping = map_headers(headers, schema)
        # Optional columns cannot dilute the recognition score of older layouts.
        required = set(schema) - ({'issuer', 'prescriber'} if kind == 'not_in_possession_medicines' else {'end_date'} if kind == 'allergies' else {'prescriber'} if kind == 'in_possession_medicines' else set())
        score = len(required.intersection(mapping)) / max(len(required), 1)
        if kind == 'not_in_possession_medicines' and 'administration_dose_time_description' in mapping:
            score += .16
        if kind == 'in_possession_medicines' and any(f in mapping for f in ('possession_quantity', 'possession_interval')):
            score += .10
        if kind == 'not_in_possession_medicines' and 'issuer' in mapping:
            score += .02
        if kind == 'allergies' and 'end_date' in mapping:
            score += .02
        if kind == 'in_possession_medicines' and ('in possession' in context and 'not in possession' not in context):
            score += 0.12
        if kind == 'not_in_possession_medicines' and ('not in possession' in context or 'non possession' in context):
            score += 0.12
        if kind == 'allergies' and ('allerg' in context or 'sensitivit' in context):
            score += 0.12
        # Avoid choosing an over-generic classification with only 'start date'.
        if kind == 'allergies' and 'allergy_or_sensitivity' not in mapping:
            score -= 0.35
        scored.append((score, kind, mapping))
    return max(scored, key=lambda x: x[0])
