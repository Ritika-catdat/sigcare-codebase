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
