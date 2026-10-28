from prescription_ocr.document_parser import Document
from prescription_ocr.output_contract import build_output, IN_FIELDS, NOT_FIELDS, ALLERGY_FIELDS
from prescription_ocr.prescription_extractor import extract
from prescription_ocr.config import Config


def test_exact_output_schema_and_confidences():
    from test_prescription_extraction import sample
    response = sample()
    internal = extract(response, {'prescription_id':'some-id'}, Config(min_table_header_score=.1))
    result = build_output(internal, Document(response), internal['extraction_metadata']['handwriting'],
                          {'prescription_id':'some-id','bucket':'b','key':'preprocessed/RX001.pdf'}, 'b', 'extracted/RX001.json')
    assert list(result) == ['patient_name','date_of_birth','age','prisoner_number','wing','Landing','cell',
                           'identification_ocr_confidence','in_possession','in_possession_record_count',
                           'not_in_possession','not_in_possession_record_count','allergy','allergy_record_count',
                           'hand_written_notes_present','hand_written_notes','notes_records_count',
                           'flag_for_review','review_reason','overall_ocr_confidence','file_name','prescription_number',
                           'original_pdf_location','prescription_location']
    assert result['patient_name'] == 'Alice'
    assert result['in_possession_record_count'] == 1
    assert result['in_possession'][0]['record_ocr_confidence'] == 99
    assert list(result['in_possession'][0]) == list(IN_FIELDS) + ['record_ocr_confidence']
    assert result['overall_ocr_confidence'] == 99
    assert result['original_pdf_location'] == ''
    assert result['prescription_number'] == ''
    assert result['prescription_location'] == 's3://b/preprocessed/RX001.pdf'


def test_record_shapes_and_handwriting_notes():
    from prescription_ocr.output_contract import _record, _handwritten_notes
    row = {'drug_name':'Medication', 'issuer':'Issuer A','prescriber_name':'Doctor B',
           '_provenance':{'drug_name':{'ocr_confidence':91},'issuer':{'ocr_confidence':87}}}
    record = _record(row, NOT_FIELDS)
    assert record['record_ocr_confidence'] == 89
    assert record['prescriber'] == ''
    assert list(record) == list(NOT_FIELDS)+['record_ocr_confidence']
    assert list(_record({}, ALLERGY_FIELDS)) == list(ALLERGY_FIELDS)+['record_ocr_confidence']
    blocks = [
        {'BlockType':'WORD','Id':'w1','Page':1,'Text':'Please','Confidence':84,'TextType':'HANDWRITING'},
        {'BlockType':'WORD','Id':'w2','Page':1,'Text':'review','Confidence':94,'TextType':'HANDWRITING'},
        {'BlockType':'LINE','Id':'l1','Page':1,'Relationships':[{'Type':'CHILD','Ids':['w1','w2']}]}]
    doc=Document({'Blocks':blocks})
    assert _handwritten_notes(doc,{'top_handwriting_word_ids':['w1','w2']}) == [
        {'note':'Please review','note_ocr_confidence':89}]


def test_separate_landing_cell_fields():
    from prescription_ocr.output_contract import _landing_and_cell
    p = {'landing':{'value':'C'},'cell':{'value':'12'},'landing_cell':{'value':'C / 12'}}
    assert _landing_and_cell(p) == ('C','12')


def test_preprocessing_metadata_is_preserved():
    from test_prescription_extraction import sample
    response = sample()
    source = {'prescription_id': 'technical-id', 'bucket': 'split-bucket',
              'key': 'split/part2.pdf', 'prescription_number': 2,
              'original_pdf_location': 's3://source-bucket/processed/multi.pdf',
              'prescription_location': 's3://split-bucket/split/part2.pdf'}
    internal = extract(response, source, Config(min_table_header_score=.1))
    result = build_output(internal, Document(response), internal['extraction_metadata']['handwriting'],
                          source, 'out', 'extracted/part2.json')
    assert result['prescription_number'] == '2'
    assert result['original_pdf_location'] == 's3://source-bucket/processed/multi.pdf'
    assert result['prescription_location'] == 's3://split-bucket/split/part2.pdf'


def test_review_reasons_all_triggers_and_boundary():
    from prescription_ocr.output_contract import _review_reasons
    vals = {'name':'', 'date_of_birth':'', 'age':'', 'prisoner_number':''}
    reasons = _review_reasons(vals, True, 89.9, [], [], [])
    assert len(reasons) == 7
    assert 'Handwritten notes present' in reasons
    assert 'Missing required patient field: prisoner_number' in reasons
    assert any('below 0.9' in r for r in reasons)
    assert any('Neither in_possession' in r for r in reasons)
    vals = {'name':'Jane','date_of_birth':'1990-01-01','age':'36','prisoner_number':'P123'}
    assert _review_reasons(vals, False, 90, [{'record_ocr_confidence':90}], [], []) == []
    assert _review_reasons(vals, False, 99, [], [{'record_ocr_confidence':90}], []) == []
    assert 'Overall OCR confidence unavailable' in _review_reasons(vals, False, '', [{}], [], [])

def test_build_output_review_reason_matches_flag():
    from test_prescription_extraction import sample
    response = sample()
    source = {'prescription_id':'test','bucket':'b','key':'p.pdf'}
    internal = extract(response, source, Config(min_table_header_score=.1))
    out = build_output(internal, Document(response), internal['extraction_metadata']['handwriting'], source, 'b','p.json')
    assert out['flag_for_review'] is True
    assert isinstance(out['review_reason'], list) and out['review_reason']
    assert any('Missing required patient field' in r for r in out['review_reason'])


def test_identification_confidence_threshold():
    from prescription_ocr.output_contract import _review_reasons
    identity = {'name': 'Jane', 'date_of_birth': '1990-01-01', 'age': '36', 'prisoner_number': 'P123'}
    record = {'record_ocr_confidence': 99}
    assert _review_reasons(identity, False, 99, [record], [], [], 90) == []
    reasons = _review_reasons(identity, False, 99, [record], [], [], 89.99)
    assert any('Identification OCR confidence below 0.9' in r for r in reasons)
    reasons = _review_reasons(identity, False, 99, [record], [], [], '')
    assert 'Identification OCR confidence unavailable' in reasons


def test_all_medication_record_confidences_must_pass():
    from prescription_ocr.output_contract import _review_reasons
    identity = {'name': 'Jane', 'date_of_birth': '1990-01-01', 'age': '36', 'prisoner_number': 'P123'}
    ip = [{'record_ocr_confidence': 95}, {'record_ocr_confidence': 89.8}]
    nip = [{'record_ocr_confidence': 90}, {'record_ocr_confidence': ''}, {'record_ocr_confidence': 75}]
    reasons = _review_reasons(identity, False, 99, ip, nip, [], 95)
    assert len(reasons) == 3
    assert 'in_possession record 2: OCR confidence below 0.9: 0.8980' in reasons
    assert 'not_in_possession record 2: OCR confidence unavailable' in reasons
    assert 'not_in_possession record 3: OCR confidence below 0.9: 0.7500' in reasons
    assert _review_reasons(identity, False, 99, [{'record_ocr_confidence': 90}], [{'record_ocr_confidence': 99}], [], 90) == []


def test_build_output_flags_low_record_confidence():
    from test_prescription_extraction import sample
    response = sample()
    source = {'prescription_id': 'test', 'bucket': 'b', 'key': 'p.pdf'}
    internal = extract(response, source, Config(min_table_header_score=.1))
    internal['in_possession_medicines'][0]['_provenance']['drug_name'] = {'ocr_confidence': 70}
    out = build_output(internal, Document(response), internal['extraction_metadata']['handwriting'], source, 'b', 'p.json')
    assert out['flag_for_review']
    assert any('in_possession record 1: OCR confidence below 0.9' in r for r in out['review_reason'])
