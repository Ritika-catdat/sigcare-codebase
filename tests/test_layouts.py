"""Header and table layouts seen on real prescriptions (Lindholme, Eastwood Park, a two-page
script with an upside-down first page and a wrapped medicine row)."""
from prescription_ocr.config import Config
from prescription_ocr.document_parser import Document
from prescription_ocr.output_contract import build_output, date_only, split_address
from prescription_ocr.patient_provider_extractor import extract_provider
from prescription_ocr.prescription_extractor import extract
from test_prescription_extraction import block


def _poly(left, top, width, height, rotation):
    # Textract starts the polygon at the text's own top-left corner.
    points = [(left, top), (left + width, top), (left + width, top + height), (left, top + height)]
    if rotation == 180:
        points = points[2:] + points[:2]
    return [{'X': x, 'Y': y} for x, y in points]


def _geometry(left, top, width, height, rotation):
    if rotation == 180:  # given upright; stored as Textract sees the upside-down scan
        left, top = 1 - left - width, 1 - top - height
    return {'BoundingBox': {'Left': left, 'Top': top, 'Width': width, 'Height': height},
            'Polygon': _poly(left, top, width, height, rotation)}


def line(blocks, ident, text, top, left=.05, page=1, rotation=0, word_ids=None):
    """A LINE with its WORDs; positions are as on the upright page."""
    ids = word_ids or []
    if word_ids is None:
        for i, word in enumerate(text.split()):
            ids.append(f'{ident}w{i}')
            blocks.append(block('WORD', ids[-1], Page=page, Text=word, TextType='PRINTED',
                                Geometry=_geometry(left + i * .03, top, .025, .015, rotation)))
    width = .03 * len(text.split())
    blocks.append(block('LINE', ident, Page=page, Text=text, Relationships=[{'Type': 'CHILD', 'Ids': ids}],
                        Geometry=_geometry(left, top, width, .015, rotation)))


def table(blocks, ident, rows, top, page=1, pitch=.022, col_width=.08, row_tops=None):
    """A TABLE whose cells hold real word boxes; each row is one printed line."""
    cells = []
    for r, texts in enumerate(rows, 1):
        row_top = row_tops[r - 1] if row_tops else top + (r - 1) * pitch
        for c, text in enumerate(texts, 1):
            ids = []
            for i, word in enumerate(text.split()):
                ids.append(f'{ident}w{r}_{c}_{i}')
                blocks.append(block('WORD', ids[-1], Page=page, Text=word, TextType='PRINTED',
                                    Geometry=_geometry(c * col_width + i * .018, row_top + .003, .015, .016, 0)))
            cells.append(f'{ident}c{r}_{c}')
            blocks.append(block('CELL', cells[-1], Page=page, RowIndex=r, ColumnIndex=c,
                                Relationships=[{'Type': 'CHILD', 'Ids': ids}],
                                Geometry=_geometry(c * col_width, row_top, col_width, pitch, 0)))
    blocks.append(block('TABLE', ident, Page=page, Relationships=[{'Type': 'CHILD', 'Ids': cells}],
                        Geometry=_geometry(col_width, top, col_width * 11, pitch * len(rows), 0)))
    return [i for i in cells]


def output(blocks, pages=1):
    response = {'Blocks': blocks, 'Pages': pages}
    internal = extract(response, {'prescription_id': 'rx'}, Config())
    return build_output(internal, Document(response), internal['extraction_metadata']['handwriting'],
                        {'prescription_id': 'rx', 'bucket': 'b', 'key': 'rx.pdf'}, 'b', 'rx.json')


ADDRESS_FIELDS = ('site_name', 'address', 'address_line_1', 'address_line_2', 'address_line_3',
                  'city', 'county', 'postcode')


def test_date_of_birth_keeps_only_the_date():
    assert date_only('19 Dec 1990 (35 y)') == '19 Dec 1990'
    assert date_only('19/12/1990 35y') == '19/12/1990'
    assert date_only('DOB 1st Sept 1990, aged 36') == '1st Sept 1990'
    assert date_only('10 Jul 1988') == '10 Jul 1988'
    assert date_only('unknown') == 'unknown'
    assert date_only(None) == ''


def test_lindholme_header_below_the_print_stamp_and_age_after_the_date_of_birth():
    blocks = []
    line(blocks, 'pn', 'Patient name: Mr Kyle Lindley A4172DD', .02, left=.03)
    line(blocks, 'stamp', '08/10/2026 13:37 / Printed by Chloe Campbell (Page 1 of 1)', .02, left=.55)
    line(blocks, 'site', 'HMP Lindholme', .055, left=.45)
    line(blocks, 'addr', 'Bawtry Road, Doncaster, South Yorkshire, DN7 6EE / 01302 524794', .085, left=.3)
    table(blocks, 'pt', [['Patient Name:', 'Mr Kyle Lindley'], ['Date of Birth:', '19 Dec 1990 (35 y)']],
          .13, col_width=.15)
    # Textract also returns the table text as lines; they are not part of the address.
    line(blocks, 'tl', 'Mr Kyle Lindley', .13, left=.3, word_ids=['ptw1_2_0', 'ptw1_2_1', 'ptw1_2_2'])
    out = output(blocks)
    assert {k: out[k] for k in ADDRESS_FIELDS} == {
        'site_name': 'HMP Lindholme', 'address': 'Bawtry Road, Doncaster, South Yorkshire, DN7 6EE',
        'address_line_1': 'HMP Lindholme', 'address_line_2': 'Bawtry Road', 'address_line_3': '',
        'city': 'Doncaster', 'county': 'South Yorkshire', 'postcode': 'DN7 6EE'}
    assert out['date_of_birth'] == '19 Dec 1990'
    assert out['date_of_birth_transformed'] == '1990-12-19'


def test_eastwood_park_site_name_at_the_end_of_the_address_line():
    blocks = []
    line(blocks, 'by', 'Printed by Miss Kay Goodall', .03, left=.05)
    line(blocks, 'pn', 'Patient name: Miss Tina Arrowsmith', .03, left=.5)
    line(blocks, 'addr', 'Wotton-under-Edge, Gloucestershire, GL12 8DB, HMP/YOI Eastwood Park', .08, left=.2)
    table(blocks, 'pt', [['Patient name:', 'Miss Tina Arrowsmith'], ['Date of birth:', '10 Jul 1988']],
          .13, col_width=.15)
    line(blocks, 'tl', 'Miss Tina Arrowsmith', .13, left=.3, word_ids=['ptw1_2_0', 'ptw1_2_1', 'ptw1_2_2'])
    line(blocks, 'td', '10 Jul 1988', .152, left=.3, word_ids=['ptw2_2_0', 'ptw2_2_1', 'ptw2_2_2'])
    out = output(blocks)
    assert {k: out[k] for k in ADDRESS_FIELDS} == {
        'site_name': 'HMP/YOI Eastwood Park', 'address': 'Wotton-under-Edge, Gloucestershire, GL12 8DB',
        'address_line_1': 'HMP/YOI Eastwood Park', 'address_line_2': '', 'address_line_3': '',
        'city': 'Wotton-under-Edge', 'county': 'Gloucestershire', 'postcode': 'GL12 8DB'}
    assert out['date_of_birth_transformed'] == '1988-07-10'


IP_HEADER = ['Drug name', 'Dose', 'Script', 'Start', 'Days', 'Quantity', 'Medication', 'Possession',
             'Possession', 'Medication', 'Prescriber']
IP_HEADER_2 = ['', '', 'Notes', 'date', 'duration', '', 'type', 'quantity', 'interval', 'notes', 'name']


def test_upside_down_first_page_and_a_medicine_row_wrapped_over_several_lines():
    blocks = []
    # Page 1 was scanned upside down: in Textract's coordinates its footer is at the top.
    for ident, text, top, left in [('by', 'Printed by Dr Nono Igwe', .03, .05),
                                   ('pn', 'Patient name: Mr Mustopha Salawudeen Adigun', .03, .5),
                                   ('site', 'HMP Belmarsh', .06, .42),
                                   ('street', 'Western Way,', .085, .35),
                                   ('addr', 'Thamesmead, London, SE28 0EB', .11, .35),
                                   ('q', 'quantity', .86, .7), ('n', 'name', .86, .85),
                                   ('date', '13/10/2026 13:58', .90, .42),
                                   ('nhs', 'NHS Confidential: Personal Data about a Patient', .93, .3),
                                   ('pg', 'Page 1 of 2', .96, .45)]:
        line(blocks, ident, text, top, left=left, rotation=180)
    # Page 2: the one medicine row came back from Textract as a row per printed line.
    table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                         ['Laxido', '1', '1 sachet, 2', '13', '10 day(s)', '19', 'Regular', '14 sachets',
                          '7 days', '', 'IGWE,'],
                         ['Orange oral', 'sachet', 'times per 1', 'Oct', '', 'sachet', '', '', '', '', 'Nono'],
                         ['powder', '', 'day(s)', '2026', '', '', '', '', '', '', ''],
                         ['sachets', '', '(08:30,', '', '', '', '', '', '', '', ''],
                         ['sugar free (Galen Ltd)', '', '19:45)', '', '', '', '', '', '', '', '']],
          .2, page=2)
    out = output(blocks, pages=2)
    assert out['site_name'] == 'HMP Belmarsh'
    assert out['address'] == 'Western Way, Thamesmead, London, SE28 0EB'
    assert (out['city'], out['county'], out['postcode']) == ('Thamesmead', 'London', 'SE28 0EB')
    assert out['in_possession_record_count'] == 1
    record = out['in_possession'][0]
    assert record['drug_name'] == 'Laxido Orange oral powder sachets sugar free (Galen Ltd)'
    assert record['dose'] == '1 sachet'
    assert record['script_notes'] == '1 sachet, 2 times per 1 day(s) (08:30, 19:45)'
    assert record['start_date'] == '13 Oct 2026'
    assert record['quantity'] == '19 sachet'
    assert record['possession_quantity'] == '14 sachets'
    assert record['prescriber_name'] == 'IGWE, Nono'


def test_page_rotation_is_read_from_the_word_polygons():
    blocks = []
    line(blocks, 'up', 'HMP Belmarsh', .06, left=.42, page=1, rotation=180)
    line(blocks, 'ok', 'HMP Belmarsh', .06, left=.42, page=2)
    doc = Document({'Blocks': blocks})
    assert (doc.page_rotation(1), doc.page_rotation(2)) == (180, 0)
    upright = doc.reading_box(doc.index['up'])
    assert round(upright['Top'], 3) == .06 and round(upright['Left'], 3) == .42


def test_word_broken_across_wrapped_lines_is_joined():
    blocks = []
    cells = table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                                 ['Aripiprazol', '10mg', '1 daily', '01 Nov 2026', '28 day(s)', '28',
                                  'Regular', '7', '7 days', '', 'Dr A'],
                                 ['e', '', '', '', '', '', '', '', '', '', '']], .2)
    # "Aripiprazol" runs to the cell's right edge, so "e" on the next line finishes the word.
    word = next(b for b in blocks if b['Id'] == 'ipw3_1_0')
    word['Geometry']['BoundingBox'].update(Left=.081, Width=.077)
    assert cells
    out = output(blocks)
    assert [r['drug_name'] for r in out['in_possession']] == ['Aripiprazole']


def test_separate_medicine_rows_are_not_merged():
    blocks = []
    # Ruled rows: each printed row has the ruling line and padding above and below it.
    table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                         ['Mirtazapine 15mg', '1 tablet', 'at night', '16 Oct 2026', '28 day(s)', '28',
                          'Regular', '7', '7 days', '', 'Dr A'],
                         ['Paracetamol', '2 tablets', 'when needed', '', '', '', '', '', '', '', '']],
          .2, row_tops=[.2, .222, .26, .30])
    out = output(blocks)
    assert [r['drug_name'] for r in out['in_possession']] == ['Mirtazapine 15mg', 'Paracetamol']


def test_footer_and_table_words_are_never_the_site():
    blocks = []
    line(blocks, 'pg', 'Page 1 of 2', .02, left=.45)
    line(blocks, 'nhs', 'NHS Confidential: Personal Data about a Patient', .05, left=.3)
    line(blocks, 'date', '13/10/2026 13:58', .08, left=.42)
    line(blocks, 'q', 'quantity', .11, left=.7)
    provider = extract_provider(Document({'Blocks': blocks}))
    assert provider['name']['value'] is None
    assert provider['address']['value'] is None


def test_address_split_recognises_counties_and_streets():
    assert split_address('HMP Ford', 'Ford Road, Ford, Arundel, West Sussex, BN18 0BX')['county'] == 'West Sussex'
    assert split_address('HMP X', 'High Street, St Albans, Herts, AL1 1AA') == {
        'address_line_1': 'HMP X', 'address_line_2': 'High Street', 'address_line_3': '',
        'city': 'St Albans', 'county': 'Herts', 'postcode': 'AL1 1AA'}
    # A street is never the city; a postcode without a comma before it is still found.
    assert split_address('HMP X', '1 Prison Road, Kent ME1 1AA') == {
        'address_line_1': 'HMP X', 'address_line_2': '1 Prison Road', 'address_line_3': '',
        'city': '', 'county': 'Kent', 'postcode': 'ME1 1AA'}
    # Not an address at all: nothing is invented.
    assert split_address('', '13/10/2026 13:58, quantity, name')['postcode'] == ''


def test_prescription_date_in_the_authorising_signature_table():
    blocks = []
    table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                         ['Duloxetine 30mg', '1 capsule', '1 daily', '16 Oct 2026', '7 day(s)', '7 capsule',
                          'Regular', '7 capsules', '7 days', '', 'Muhammad Malik']], .2)
    # The prescriber signs in a table: the date is in its "Date" column, not under a label.
    table(blocks, 'sig', [['Authorising Signature', 'Print Name', 'Registration Number', 'Date'],
                          ['', '', '', '09 Oct 2026']], .7, col_width=.2)
    line(blocks, 'nhs', 'NHS Confidential: Personal Data about a Patient', .8)
    out = output(blocks)
    assert out['prescription_date'] == '09 Oct 2026'
    assert out['prescription_date_transformed']


def test_handwritten_correction_in_a_table_cell_is_kept_and_flagged():
    blocks = []
    table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                         ['Duloxetine 30mg', '1 capsule', '1 daily', '16 Oct 2026', '28 day(s) 7', '287 capsule',
                          'Regular', '7 capsules', '7 days', '', 'Muhammad Malik']], .2)
    # The prescriber struck out the printed 28 and wrote 7 beside it.
    for ident in ('ipw3_5_2', 'ipw3_6_0'):
        next(b for b in blocks if b['Id'] == ident)['TextType'] = 'HANDWRITING'
    out = output(blocks)
    record = out['in_possession'][0]
    assert record['days_duration'] == '28 day(s) [handwritten: 7]'
    assert record['quantity'] == '28 capsule [handwritten: 7]'
    assert record['possession_quantity'] == '7 capsules'
    assert out['flag_for_review']
    assert any('Handwritten text in table' in r and 'days_duration "7"' in r for r in out['review_reason'])
    assert not out['hand_written_notes_present']


def test_handwritten_number_without_a_struck_printed_number_is_not_split():
    blocks = []
    table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                         ['Duloxetine 30mg', '1 capsule', '1 daily', '16 Oct 2026', '28 day(s)', '14 capsule',
                          'Regular', '7 capsules', '7 days', '', 'Muhammad Malik']], .2)
    next(b for b in blocks if b['Id'] == 'ipw3_6_0')['TextType'] = 'HANDWRITING'
    record = output(blocks)['in_possession'][0]
    assert record['quantity'] == 'capsule [handwritten: 14]'
