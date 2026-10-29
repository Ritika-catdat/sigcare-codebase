from prescription_ocr.schemas import classify, map_headers, TABLES
from prescription_ocr.document_parser import Document
from prescription_ocr.prescription_extractor import extract
from prescription_ocr.config import Config


def block(t, ident, **kwargs):
    return {'BlockType': t, 'Id': ident, 'Page': 1, 'Confidence': 99, **kwargs}


def sample():
    words = [block('WORD','w1',Text='Patient',TextType='PRINTED'),
             block('WORD','w2',Text='Alice',TextType='PRINTED'),
             block('WORD','w3',Text='Drug',TextType='PRINTED'),
             block('WORD','w4',Text='Name',TextType='PRINTED'),
             block('WORD','w5',Text='Dose',TextType='PRINTED'),
             block('WORD','w6',Text='Paracetamol',TextType='PRINTED'),
             block('WORD','w7',Text='500mg',TextType='PRINTED')]
    def cell(ident, r, c, ids):
        return block('CELL',ident, RowIndex=r, ColumnIndex=c,
                     Relationships=[{'Type':'CHILD','Ids':ids}])
    cells = [cell('c1',1,1,['w3','w4']), cell('c2',1,2,['w5']),
             cell('c3',2,1,['w6']), cell('c4',2,2,['w7'])]
    table = block('TABLE','t1',Relationships=[{'Type':'CHILD','Ids':[c['Id'] for c in cells]}])
    key = block('KEY_VALUE_SET','key',EntityTypes=['KEY'],Relationships=[{'Type':'CHILD','Ids':['w1']},{'Type':'VALUE','Ids':['val']}])
    val = block('KEY_VALUE_SET','val',EntityTypes=['VALUE'],Relationships=[{'Type':'CHILD','Ids':['w2']}])
    return {'Blocks':words+cells+[table,key,val], 'Pages':1, 'JobId':'mock'}


def test_header_mapping():
    headers = ['Drug Name','Dose','Script Notes','Start Date','Days Duration','Quantity','Medication Type','Possession Quantity','Possession Interval','Medication Notes','Prescriber Name']
    score, kind, mapping = classify(headers, 'In Possession Medicines')
    assert kind == 'in_possession_medicines'
    assert len(mapping) == 11
    assert mapping['quantity'] == 5
    assert mapping['possession_quantity'] == 7


def test_allergy_and_nonposs():
    assert classify(['Start Date','Allergy or Sensitivity'],'Allergies')[1] == 'allergies'
    assert classify(['Drug Name','Administration Dose / Time Description','Start Date','End Date','Days Duration','Quantity','Medication Notes','Prescriber Name'],'Not in Possession')[1] == 'not_in_possession_medicines'


def test_extract_mock():
    response=sample()
    result=extract(response, {'prescription_id':'test'}, Config(min_table_header_score=.1))
    assert result['patient']['name']['value'] == 'Alice'
    assert result['in_possession_medicines'][0]['drug_name'] == 'Paracetamol'
    assert result['in_possession_medicines'][0]['dose'] == '500mg'
    assert result['in_possession_medicines'][0]['_provenance']['drug_name']['word_ids'] == ['w6']


def test_optional_columns_and_arbitrary_table_order():
    from prescription_ocr.medication_table_extractor import extract_tables
    from prescription_ocr.schemas import classify
    assert classify(['Drug Name', 'Administration Dose / Time Description', 'Issuer', 'Prescriber'], 'Not in Possession')[1] == 'not_in_possession_medicines'
    assert classify(['Start Date', 'Allergy or Sensitivity', 'End Date'], 'Allergies')[1] == 'allergies'
    assert classify(['Drug Name', 'Dose', 'Medication Type', 'Possession Quantity'], 'In Possession')[1] == 'in_possession_medicines'
    blocks = []
    idx = 0
    layouts = [
        ('allergies', ['Start Date', 'Allergy or Sensitivity', 'End Date'], ['01/01/2026', 'Penicillin', '02/01/2026']),
        ('not', ['Drug Name', 'Administration Dose / Time Description', 'Issuer', 'Prescriber'], ['Drug A','Daily','Issuer X','Dr Y']),
        ('in', ['Drug Name', 'Dose', 'Medication Type', 'Possession Quantity'], ['Drug B', '10mg','IP','7'])
    ]
    for page, (_, heads, values) in enumerate(layouts, 1):
        cells=[]
        for row, texts in enumerate([heads, values],1):
            for col, txt in enumerate(texts,1):
                idx+=1
                word=block('WORD',f'w{idx}', Page=page, Text=txt, TextType='PRINTED')
                cell=block('CELL',f'c{idx}',Page=page,RowIndex=row,ColumnIndex=col,Relationships=[{'Type':'CHILD','Ids':[word['Id']]}])
                blocks.extend([word,cell]);cells.append(cell['Id'])
        blocks.append(block('TABLE',f't{page}',Page=page,Relationships=[{'Type':'CHILD','Ids':cells}],Geometry={'BoundingBox':{'Top':.4}}))
    response={'Blocks':blocks,'Pages':3}
    result=extract(response,{'prescription_id':'mix'},Config(min_table_header_score=.4))
    assert result['allergies'][0]['end_date']=='02/01/2026'
    assert result['not_in_possession_medicines'][0]['issuer']=='Issuer X'
    assert result['not_in_possession_medicines'][0]['prescriber']=='Dr Y'
    assert result['in_possession_medicines'][0]['drug_name']=='Drug B'


def test_landing_in_patient_table_and_combined_cell():
    from prescription_ocr.patient_provider_extractor import extract_patient
    for paired in [True, False]:
        blocks=[]
        texts = [('Landing', 'A Wing / Cell 4')] if paired else [('Landing: A Wing / Cell 4', '')]
        cells=[]
        for col, txt in enumerate(texts[0],1):
            if not txt: continue
            w=block('WORD',f'w{col}',Text=txt)
            c=block('CELL',f'c{col}',RowIndex=1,ColumnIndex=col,Relationships=[{'Type':'CHILD','Ids':[w['Id']]}])
            blocks.extend([w,c]);cells.append(c['Id'])
        blocks.append(block('TABLE','table',Relationships=[{'Type':'CHILD','Ids':cells}]))
        assert extract_patient(Document({'Blocks':blocks}))['landing_cell']['value']=='A Wing / Cell 4'


def _table(blocks, ident, page, rows, top=.4):
    """Append a TABLE with one WORD per cell; rows is a list of lists of cell texts."""
    cells = []
    for r, texts in enumerate(rows, 1):
        for c, txt in enumerate(texts, 1):
            cid = f'{ident}c{r}_{c}'
            ids = []
            if txt:
                ids = [f'{ident}w{r}_{c}']
                blocks.append(block('WORD', ids[0], Page=page, Text=txt, TextType='PRINTED'))
            blocks.append(block('CELL', cid, Page=page, RowIndex=r, ColumnIndex=c,
                                Relationships=[{'Type': 'CHILD', 'Ids': ids}]))
            cells.append(cid)
    blocks.append(block('TABLE', ident, Page=page, Relationships=[{'Type': 'CHILD', 'Ids': cells}],
                        Geometry={'BoundingBox': {'Top': top, 'Left': 0, 'Width': 1, 'Height': .2}}))


IP_HEADER = ['Drug name', 'Dose', 'Script', 'Start', 'Days', 'Quantity', 'Medication', 'Possession', 'Possession', 'Medication', 'Prescriber']
IP_HEADER_2 = ['', '', 'Notes', 'date', 'duration', '', 'type', 'quantity', 'interval', 'notes', 'name']


def test_in_possession_table_split_over_two_pages_gives_one_record():
    blocks = []
    # Page 2: only the (two-line) header fits; page 3: header repeated + the medicine row.
    _table(blocks, 'ta', 2, [IP_HEADER, IP_HEADER_2, ['', '|', '', '', '', '', '', '', '', '', '']])
    _table(blocks, 'tb', 3, [IP_HEADER, IP_HEADER_2,
                             ['Laxido Orange', '1 sachet', '1 sachet, 2 times', '13 Oct 2026', '10 day(s)',
                              '19 sachet', 'Regular', '14 sachets', '7 days', '', 'IGWE, Nono']])
    result = extract({'Blocks': blocks, 'Pages': 3}, {'prescription_id': 'split'}, Config())
    records = result['in_possession_medicines']
    assert [r['drug_name'] for r in records] == ['Laxido Orange']
    assert records[0]['possession_interval'] == '7 days'


def test_row_continued_on_next_page_is_merged_into_previous_record():
    blocks = []
    _table(blocks, 'ta', 1, [['Drug name', 'Dose', 'Medication type', 'Possession quantity', 'Medication notes'],
                             ['Laxido', '1 sachet', 'Regular', '14 sachets', 'Take with']])
    _table(blocks, 'tb', 2, [['', '', '', '', 'plenty of water']])
    result = extract({'Blocks': blocks, 'Pages': 2}, {'prescription_id': 'cont'}, Config())
    records = result['in_possession_medicines']
    assert len(records) == 1
    assert records[0]['medication_notes'] == 'Take with plenty of water'


def _line(blocks, ident, text, top, left=.05, page=1, hand=False):
    ids = []
    for i, word in enumerate(text.split()):
        wid = f'{ident}w{i}'
        blocks.append(block('WORD', wid, Page=page, Text=word,
                            TextType='HANDWRITING' if hand else 'PRINTED',
                            Geometry={'BoundingBox': {'Top': top, 'Left': left + i * .05, 'Width': .04, 'Height': .02}}))
        ids.append(wid)
    blocks.append(block('LINE', ident, Page=page, Text=text,
                        Relationships=[{'Type': 'CHILD', 'Ids': ids}],
                        Geometry={'BoundingBox': {'Top': top, 'Left': left, 'Width': .3, 'Height': .02}}))


def test_signature_is_not_a_handwritten_note():
    from prescription_ocr.handwriting_detector import detect_handwriting
    blocks = []
    _line(blocks, 'printed', 'Printed by Dr Nono Igwe', .05)
    _line(blocks, 'sig', 'Dr lgwe', .22, left=.4, hand=True)          # signature showing through
    _line(blocks, 'label', 'Signed:', .70, page=1)
    _line(blocks, 'sig2', 'Qi', .67, left=.2, hand=True)              # scrawl above "Signed:"
    doc = Document({'Blocks': blocks})
    result = detect_handwriting(doc, [], Config())
    assert result['note_word_ids'] == []
    assert result['handwritten_notes'] is False
    assert set(result['signature_word_ids']) == {'sigw0', 'sigw1', 'sig2w0'}


def test_real_handwritten_note_is_still_reported():
    from prescription_ocr.handwriting_detector import detect_handwriting
    blocks = []
    _line(blocks, 'printed', 'Printed by Dr Nono Igwe', .05)
    _line(blocks, 'note', 'Patient refused dose', .15, left=.4, hand=True)
    result = detect_handwriting(Document({'Blocks': blocks}), [], Config())
    assert result['handwritten_notes'] is True
    assert result['signature_word_ids'] == []


def test_blank_landing_does_not_take_the_cell_label_below_it():
    from prescription_ocr.patient_provider_extractor import extract_patient
    blocks = []
    _table(blocks, 'pt', 1, [['Patient name:', 'Mr A'], ['Prisoner number:', '111'],
                             ['Landing:', ''], ['Cell:', 'D103']], top=.1)
    patient = extract_patient(Document({'Blocks': blocks}))
    assert patient['landing']['value'] is None
    assert patient['cell']['value'] == 'D103'


def test_signature_read_as_a_single_letter_is_not_a_note():
    from prescription_ocr.handwriting_detector import detect_handwriting
    from prescription_ocr.output_contract import _handwritten_notes
    blocks = []
    _line(blocks, 'printed', 'Printed by David Antwi-Boasiako', .05)
    _line(blocks, 'sig', 'a', .12, left=.7, hand=True)   # signature showing through, no label read
    doc = Document({'Blocks': blocks})
    result = detect_handwriting(doc, [], Config())
    assert result['note_word_ids'] == []
    assert _handwritten_notes(doc, result) == []


def test_sign_off_and_possession_status_boxes_are_ignored():
    blocks = []
    _table(blocks, 'ta', 1, [['Drug name', 'Dose', 'Medication type', 'Possession quantity'],
                             ['Laxido', '1 sachet', 'Regular', '14 sachets']])
    _table(blocks, 'tb', 1, [['Clinically screened by:', ''], ['Dispensed by:', ''], ['Checked by:', '']], top=.7)
    _table(blocks, 'tc', 1, [['Medication Possession Status:'], ['Not in possession']], top=.1)
    result = extract({'Blocks': blocks, 'Pages': 1}, {'prescription_id': 'boxes'}, Config())
    reasons = result['extraction_metadata']['review_reasons']
    assert not any('Unclassified table' in r for r in reasons)
    assert [r['drug_name'] for r in result['in_possession_medicines']] == ['Laxido']


def test_unrecognised_table_with_a_drug_column_is_still_flagged():
    blocks = []
    _table(blocks, 'ta', 1, [['Drug name', 'Strength'], ['Laxido', '1 sachet']])
    result = extract({'Blocks': blocks, 'Pages': 1}, {'prescription_id': 'odd'}, Config(min_table_header_score=.95))
    assert any('Unclassified table' in r for r in result['extraction_metadata']['review_reasons'])


def test_handwritten_note_beside_the_signature_is_captured():
    from prescription_ocr.handwriting_detector import detect_handwriting
    from prescription_ocr.output_contract import _handwritten_notes
    blocks = []
    _line(blocks, 'printed', 'Printed by Mrs Mary-Jayne Bosley', .02)
    _line(blocks, 'label', 'Signed: ....................', .80)
    _line(blocks, 'sig', 'M', .78, left=.25, hand=True)                          # signature scrawl
    _line(blocks, 'n1', 'This is not a duplicate', .76, left=.55, hand=True)    # note at the bottom
    _line(blocks, 'n2', 'Please dispense 7 days I.P.', .80, left=.55, hand=True)
    _line(blocks, 'n3', 'But not blister packs. Thank you.', .84, left=.55, hand=True)
    doc = Document({'Blocks': blocks})
    result = detect_handwriting(doc, [], Config())
    assert result['handwritten_notes'] is True
    assert 'sigw0' in result['signature_word_ids']
    assert [n['note'] for n in _handwritten_notes(doc, result)] == [
        'This is not a duplicate', 'Please dispense 7 days I.P.', 'But not blister packs. Thank you.']


def test_handwriting_inside_a_table_is_not_a_note():
    from prescription_ocr.handwriting_detector import detect_handwriting
    blocks = []
    _line(blocks, 'strike', 'struck through', .45, hand=True)
    blocks.append(block('TABLE', 't1', Geometry={'BoundingBox': {'Top': .4, 'Left': 0, 'Width': 1, 'Height': .2}}))
    result = detect_handwriting(Document({'Blocks': blocks}), [], Config())
    assert result['note_word_ids'] == []


def _word(blocks, ident, text, left, top, width, height=.015, page=1):
    blocks.append(block('WORD', ident, Page=page, Text=text, TextType='PRINTED',
                        Geometry={'BoundingBox': {'Left': left, 'Top': top, 'Width': width, 'Height': height}}))
    return ident


def test_word_run_across_a_column_line_is_split_between_the_cells():
    blocks = []
    def cell(ident, r, c, left, width, ids, top=.30, height=.10):
        blocks.append(block('CELL', ident, RowIndex=r, ColumnIndex=c, Relationships=[{'Type': 'CHILD', 'Ids': ids}],
                            Geometry={'BoundingBox': {'Left': left, 'Top': top, 'Width': width, 'Height': height}}))
        return ident
    # Dose column 0.10-0.20, Script Notes 0.20-0.30. Line 1 at top .31, line 2 at .33.
    ids = [cell('h1', 1, 1, .10, .10, [_word(blocks, 'hw1', 'Dose', .11, .21, .05)], top=.20),
           cell('h2', 1, 2, .20, .10, [_word(blocks, 'hw2', 'Script', .21, .21, .05),
                                       _word(blocks, 'hw3', 'Notes', .21, .23, .05)], top=.20),
           cell('d', 2, 1, .10, .10, [_word(blocks, 'w1', '1', .11, .31, .01),
                                      _word(blocks, 'w2', 'tabletitimes', .11, .33, .14)]),
           cell('s', 2, 2, .20, .10, [_word(blocks, 'w3', '1', .21, .31, .01),
                                      _word(blocks, 'w4', 'tablet,', .225, .31, .04),
                                      _word(blocks, 'w5', '1', .27, .31, .01),
                                      _word(blocks, 'w6', 'per', .26, .33, .02),
                                      _word(blocks, 'w7', '1', .21, .35, .01),
                                      _word(blocks, 'w8', 'day(s)', .225, .35, .04)])]
    blocks.append(block('TABLE', 't1', Relationships=[{'Type': 'CHILD', 'Ids': ids}]))
    grid = Document({'Blocks': blocks}).table_grid(blocks[-1])
    assert grid[(2, 1)].text == '1 tablet'
    assert grid[(2, 2)].text == '1 tablet, 1 times per 1 day(s)'


def test_age_without_a_number_is_read_from_the_row_beside_the_label():
    from prescription_ocr.patient_provider_extractor import extract_patient
    blocks = []
    # The table cell only has "y"; the full "36 y" sits on the row to the right of the label.
    _table(blocks, 'pt', 1, [['Patient name:', 'Mr Jake Prempeh'], ['Date of birth:', '06 Jul 1990'],
                             ['Patient age:', 'y'], ['Prisoner number:', 'A4094AG']], top=.1)
    _line(blocks, 'age', 'Patient age:', .20, left=.05)                # label line, right edge .35
    _word(blocks, 'a1', '36', .36, .20, .02)
    _word(blocks, 'a2', 'y', .385, .20, .01)
    patient = extract_patient(Document({'Blocks': blocks}))
    assert patient['age']['value'] == '36 y'
    assert patient['date_of_birth']['value'] == '06 Jul 1990'
    assert patient['prisoner_number']['value'] == 'A4094AG'


def test_letters_only_patient_values_are_kept_when_nothing_better_exists():
    from prescription_ocr.patient_provider_extractor import extract_patient
    blocks = []
    _table(blocks, 'pt', 1, [['Patient name:', 'Mr A'], ['Patient age:', 'Unknown'],
                             ['Prisoner number:', 'Not recorded']], top=.1)
    patient = extract_patient(Document({'Blocks': blocks}))
    assert patient['age']['value'] == 'Unknown'
    assert patient['prisoner_number']['value'] == 'Not recorded'
