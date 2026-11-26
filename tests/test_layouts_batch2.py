"""Layouts from the second batch of real prescriptions: upside-down narrow tables (Fosse Way,
Kirkham), tinted pages with tight rows (Eastwood Park), no Dose column (Huntercombe), a
prison named after a street, and handwriting across the address (Grendon)."""
from prescription_ocr.document_parser import Document
from test_layouts import IP_HEADER, IP_HEADER_2, _geometry, line, output, table


def _place(blocks, ident, left, top, width, rotation=0, height=.016):
    """Move a word to an upright position (stored as Textract sees the rotated scan)."""
    word = next(b for b in blocks if b['Id'] == ident)
    word['Geometry'] = _geometry(left, top, width, height, rotation)


def test_words_broken_across_lines_are_joined_on_an_upside_down_page():
    blocks = []
    table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                         ['Dapagliflozi n 10mg tablets', '1 tablet', 'daily', '15 Oct 202 6', '28 day(s)',
                          '28 tablets', 'Regular', '28 tablets', '28 days', '', 'Ms Stacy Collins']],
          .2, pitch=.07, rotation=180)
    # Narrow columns wrap by character: "Dapagliflozi" fills the first line of its cell
    # (.08 to .16) and "n" starts the next one; the year is cut the same way.
    _place(blocks, 'ipw3_1_0', .081, .343, .078, 180)
    _place(blocks, 'ipw3_1_1', .081, .362, .006, 180)
    _place(blocks, 'ipw3_1_2', .09, .362, .03, 180)
    _place(blocks, 'ipw3_1_3', .081, .381, .04, 180)
    _place(blocks, 'ipw3_4_2', .375, .343, .024, 180)
    _place(blocks, 'ipw3_4_3', .321, .362, .008, 180)
    line(blocks, 'pn', 'Patient name: Mr Richard Osborne, A5595AL', .05, rotation=180)
    out = output(blocks)
    record = out['in_possession'][0]
    assert record['drug_name'] == 'Dapagliflozin 10mg tablets'
    assert record['start_date'] == '15 Oct 2026'
    assert out['patient_name'] == 'Mr Richard Osborne'


def test_line_breaks_inside_a_cell():
    doc = Document({'Blocks': []})
    cell = {'Left': 0, 'Top': 0, 'Width': .1, 'Height': .1}

    def joined(first, second):
        # The first piece runs to the cell's right edge; the second starts the next line.
        width = .099
        return doc._join_lines([({'Left': .1 - width, 'Top': 0, 'Width': width, 'Height': .01}, first),
                                ({'Left': 0, 'Top': .012, 'Width': .01, 'Height': .01}, second)], cell)

    # A word or number the printer cut at the edge is put back together...
    assert joined('Dapagliflozi', 'n') == 'Dapagliflozin'
    assert joined('202', '6') == '2026'
    assert joined('gastro-', 'resistant') == 'gastro-resistant'
    assert joined('Propranolo', 'I') == 'Propranolol'
    assert joined('capsule', 'S') == 'capsules'
    # ...but a line that ended at a word keeps the space.
    assert joined('50microgram', 'tablets') == '50microgram tablets'
    assert joined('2.5mg', 'tablets') == '2.5mg tablets'
    assert joined('(Aspire', 'Pharma') == '(Aspire Pharma'
    assert joined('UK', 'Ltd)') == 'UK Ltd)'


def test_next_medicine_right_under_a_longer_row_is_not_merged():
    blocks = []
    # Rows sit tight together, and the first has a notes value the second has not.
    table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                         ['Orlistat 120mg capsules', '1 capsule', '3 times', '28 Oct 2026', '28 day(s)',
                          '84 capsule', 'Regular', '84 capsules', '28 days', 'Target weight', 'Dr Ruth Lloyd'],
                         ['Amlodipine 10mg tablets', '1 tablet', '1 times', '28 Oct 2026', '28 day(s)',
                          '28 tablet', 'Regular', '28 tablets', '28 days', '', 'Mrs Rebecca Boeater']], .2)
    out = output(blocks)
    assert [r['drug_name'] for r in out['in_possession']] == ['Orlistat 120mg capsules', 'Amlodipine 10mg tablets']


def test_layout_without_a_dose_column_is_not_flagged_for_a_missing_dose():
    blocks = []
    header = ['Drug name', 'Script Notes', 'Start date', 'Days duration', 'Quantity', 'Medication type',
              'Possession quantity', 'Possession interval', 'Medication notes', 'Prescriber name']
    table(blocks, 'ip', [header, ['Amlodipine 5mg tablets', '1 tablet daily', '20 Oct 2026', '28 day(s)',
                                  '28 tablet', 'Regular', '28 tablets', '28 days', '', 'Dr Rachel Kavalier']],
          .2)
    out = output(blocks)
    assert out['in_possession'][0]['drug_name'] == 'Amlodipine 5mg tablets'
    assert not any('dose' in reason for reason in out['review_reason'])


def test_prison_name_wins_over_a_department_in_the_address_line():
    blocks = []
    line(blocks, 'site', 'HMP Huntercombe', .05, left=.4)
    line(blocks, 'addr', 'Healthcare Centre, Nuffield, Henley-on-Thames, Oxfordshire, RG9 5SB', .075, left=.2)
    out = output(blocks)
    assert out['site_name'] == 'HMP Huntercombe'
    assert out['address'] == 'Healthcare Centre, Nuffield, Henley-on-Thames, Oxfordshire, RG9 5SB'
    assert (out['city'], out['county'], out['postcode']) == ('Henley-on-Thames', 'Oxfordshire', 'RG9 5SB')


def test_prison_named_after_a_street_is_the_site_and_not_repeated_in_the_address():
    blocks = []
    line(blocks, 'site', 'HMP Fosse Way,', .05, left=.4)
    line(blocks, 'addr', 'HMP Fosse Way, Tigers Road, Wigston, LE18 4WS', .075, left=.3)
    out = output(blocks)
    assert out['site_name'] == 'HMP Fosse Way'
    assert out['address'] == 'Tigers Road, Wigston, LE18 4WS'
    assert (out['address_line_2'], out['city'], out['postcode']) == ('Tigers Road', 'Wigston', 'LE18 4WS')


def test_handwriting_across_the_address_line_is_left_out():
    blocks = []
    line(blocks, 'site', 'HMP Grendon', .05, left=.4)
    line(blocks, 'addr', 'Grendon, Grendon Underwood, Aylesbury, Buckinghamshire, HP18 0TL Dossett', .075,
         left=.2)
    next(b for b in blocks if b['Id'] == 'addrw7')['TextType'] = 'HANDWRITING'
    out = output(blocks)
    assert out['site_name'] == 'HMP Grendon'
    assert out['address'] == 'Grendon, Grendon Underwood, Aylesbury, Buckinghamshire, HP18 0TL'
