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
                           'flag_for_review','review_reasons','overall_ocr_confidence','file_name','prescription_number',
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


def test_review_reasons_only_when_flagged():
    from test_prescription_extraction import sample
    response = sample()
    source = {'prescription_id': 'id', 'bucket': 'b', 'key': 'split/rx.pdf'}
    internal = extract(response, source, Config(min_table_header_score=.1))
    handwriting = internal['extraction_metadata']['handwriting']

    internal['extraction_metadata'].update(status='EXTRACTED', review_reasons=['stale reason'])
    clean = build_output(internal, Document(response), handwriting, source, 'b', 'out.json')
    assert clean['flag_for_review'] is False and clean['review_reasons'] == []

    internal['extraction_metadata'].update(status='REVIEW_REQUIRED', review_reasons=['Low OCR confidence'])
    flagged = build_output(internal, Document(response), handwriting, source, 'b', 'out.json')
    assert flagged['flag_for_review'] is True and flagged['review_reasons'] == ['Low OCR confidence']
