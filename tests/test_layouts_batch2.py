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


def _block(kind, ident, **extra):
    from test_prescription_extraction import block
    return block(kind, ident, **extra)


def test_upside_down_patient_table_numbered_in_scan_order():
    # Textract numbered this table's rows and columns in scan order: on the upright page
    # the label column is column 2 and the rows run bottom-up.
    blocks, cells = [], []
    rows = [('Prisoner number:', 'A7701CN'), ('Date of birth:', '20 Sep 1983'), ('Patient name:', 'Mr Ali Khan')]
    for upright_row, (label, value) in enumerate(rows):
        for upright_col, text in enumerate((label, value)):
            r, c = len(rows) - upright_row, 2 - upright_col
            ids = []
            for i, word in enumerate(text.split()):
                ids.append(f'w{r}{c}{i}')
                blocks.append(_block('WORD', ids[-1], Page=1, Text=word, TextType='PRINTED',
                                     Geometry=_geometry(.1 + upright_col * .3 + i * .05, .2 + upright_row * .03,
                                                        .04, .015, 180)))
            cells.append(f'c{r}{c}')
            blocks.append(_block('CELL', cells[-1], Page=1, RowIndex=r, ColumnIndex=c,
                                 Relationships=[{'Type': 'CHILD', 'Ids': ids}],
                                 Geometry=_geometry(.1 + upright_col * .3, .2 + upright_row * .03, .3, .03, 180)))
    blocks.append(_block('TABLE', 't', Page=1, Relationships=[{'Type': 'CHILD', 'Ids': cells}],
                         Geometry=_geometry(.1, .2, .6, .09, 180)))
    out = output(blocks)
    assert (out['patient_name'], out['date_of_birth'], out['prisoner_number']) == (
        'Mr Ali Khan', '20 Sep 1983', 'A7701CN')


def test_medicine_table_found_only_as_layout_is_rebuilt_from_its_lines():
    blocks = []
    line(blocks, 'title', 'IN Possession Prescriptions', .27)
    ids = []
    for ident, text, top, left in [('h1', 'Drug name', .3, .05), ('h2', 'Dose', .3, .2), ('h3', 'Script Notes', .3, .3),
                                   ('h4', 'Start date', .3, .45), ('h5', 'Quantity', .3, .6),
                                   ('h6', 'Prescriber', .3, .75),
                                   ('a1', 'Amlodipine 5mg', .33, .05), ('a2', '1 tablet', .33, .2),
                                   ('a3', 'daily', .33, .3), ('a4', '20 Oct 2026', .33, .45),
                                   ('a5', '28 tablet', .33, .6), ('a6', 'Dr A', .33, .75),
                                   ('a7', 'tablets', .345, .05),
                                   ('b1', 'Paracetamol 500mg', .37, .05), ('b2', '2 tablets', .37, .2),
                                   ('b3', 'when needed', .37, .3), ('b4', '20 Oct 2026', .37, .45),
                                   ('b5', '32 tablet', .37, .6), ('b6', 'Dr A', .37, .75)]:
        line(blocks, ident, text, top, left=left)
        ids.append(ident)
    # Textract marked the area as a table in the page layout but returned no cells.
    blocks.append(_block('LAYOUT_TABLE', 'lt', Page=1, Relationships=[{'Type': 'CHILD', 'Ids': ids}],
                         Geometry=_geometry(.04, .29, .9, .1, 0)))
    out = output(blocks)
    assert [(r['drug_name'], r['dose'], r['start_date'], r['quantity']) for r in out['in_possession']] == [
        ('Amlodipine 5mg tablets', '1 tablet', '20 Oct 2026', '28 tablet'),
        ('Paracetamol 500mg', '2 tablets', '20 Oct 2026', '32 tablet')]


def test_prescription_date_under_the_signature_on_an_upside_down_page():
    blocks = []
    line(blocks, 'signed', 'Signed:', .85, rotation=180)
    line(blocks, 'date', 'Date: 09 October 2026', .87, rotation=180)
    assert output(blocks)['prescription_date'] == '09 October 2026'


def test_whole_word_at_a_line_end_is_not_joined_to_the_next_word():
    doc = Document({'Blocks': []})
    cell = {'Left': 0, 'Top': 0, 'Width': .1, 'Height': .1}
    pieces = [({'Left': .03, 'Top': 0, 'Width': .069, 'Height': .01}, 'Ferrous'),
              ({'Left': .03, 'Top': .012, 'Width': .069, 'Height': .01}, 'fumarat'),
              ({'Left': 0, 'Top': .024, 'Width': .01, 'Height': .01}, 'e')]
    assert doc._join_lines(pieces, cell) == 'Ferrous fumarate'


def test_two_medicines_textract_returned_as_one_row_are_split():
    # Eastwood Park: Textract missed the ruling line between Mirtazapine and Lansoprazole and
    # returned one row holding both. Each cell's text runs over several printed lines.
    header = ['Drug name', 'Dose', 'Script Notes', 'Start date', 'Days duration', 'Quantity',
              'Medication type', 'Possession quantity', 'Possession interval', 'Medication notes',
              'Prescriber name']
    first = [['Mirtazapine', '30mg tablets'], ['1', 'tablet'], ['1 tablet,', 'times per', '1 day(s)', '(17:30)'],
             ['28', 'Oct', '2026'], ['28', 'day(s)'], ['28 tablet'], ['Regular'], ['7 tablets'], ['7 days'],
             ['take at night', 'if possible'], ['Dr Louise', 'Stranks']]
    second = [['Lansoprazole', '30mg gastro-', 'resistant', 'capsules'], ['1', 'capsule'],
              ['1 capsule,', '1 times', 'per 1', 'day(s)', '(08:00)'], ['28', 'Oct', '2026'], ['28', 'day(s)'],
              ['28 capsule'], ['Regular'], ['7 capsules'], ['7 days'], [], ['Dr Louise', 'Stranks']]
    blocks, cells, width, pitch = [], [], .085, .02

    def cell(ident, r, c, lines, top, height, extra=()):
        ids = []
        for n, text in enumerate(lines):
            for i, word in enumerate(text.split()):
                ids.append(f'{ident}_{n}_{i}')
                blocks.append(_block('WORD', ids[-1], Page=1, Text=word, TextType='PRINTED',
                                     Geometry=_geometry(.02 + c * width + .002 + i * .03, top + n * pitch,
                                                        .028, .014, 0)))
        for ident_, word, left in extra:
            ids.insert(1, ident_)
            blocks.append(_block('WORD', ident_, Page=1, Text=word, TextType='PRINTED',
                                 Geometry=_geometry(left, top, .006, .014, 0)))
        cells.append(ident)
        blocks.append(_block('CELL', ident, Page=1, RowIndex=r, ColumnIndex=c + 1,
                             Relationships=[{'Type': 'CHILD', 'Ids': ids}],
                             Geometry=_geometry(.02 + c * width, top - .003, width, height, 0)))

    for c, text in enumerate(header):
        cell(f'h{c}', 1, c, [text], .2, .03)
    for c in range(len(header)):
        # The last "1" of "1 tablet, 1" ran into the Start date column's left edge.
        extra = [('stray1', '1', .02 + c * width - .005)] if c == 3 else ()
        cell(f'd{c}', 2, c, first[c] + [''] * (4 - len(first[c])) + second[c], .235, .21, extra)
    blocks.append(_block('TABLE', 't', Page=1, Relationships=[{'Type': 'CHILD', 'Ids': cells}],
                         Geometry=_geometry(.02, .197, width * 11, .25, 0)))
    line(blocks, 'title', 'IN Possession Prescriptions', .17)
    rows = output(blocks)['in_possession']
    assert [(r['drug_name'], r['dose'], r['start_date'], r['quantity'], r['medication_type'],
             r['possession_interval']) for r in rows] == [
        ('Mirtazapine 30mg tablets', '1 tablet', '28 Oct 2026', '28 tablet', 'Regular', '7 days'),
        ('Lansoprazole 30mg gastro-resistant capsules', '1 capsule', '28 Oct 2026', '28 capsule', 'Regular',
         '7 days')]
    assert rows[0]['script_notes'] == '1 tablet, 1 times per 1 day(s) (17:30)'
    assert rows[0]['medication_notes'] == 'take at night if possible' and not rows[1]['medication_notes']


def test_cell_lines_listed_out_of_order_on_an_upside_down_page_are_read_in_order():
    # Kirkham: Textract listed some cells' lines last line first ("10mg tablets" before
    # "Dapagliflozin"), header cells included, so the columns were not recognised.
    blocks = []
    table(blocks, 'ip', [IP_HEADER, IP_HEADER_2,
                         ['Dapagliflozin 10mg tablets', '1 tablet', 'daily', '15 Oct 2026', '28 day(s)',
                          '28 tablets', 'Regular', '28 tablets', '28 days', '', 'Ms Stacy Collins']],
          .2, pitch=.07, rotation=180)
    _place(blocks, 'ipw3_1_0', .081, .343, .05, 180)
    _place(blocks, 'ipw3_1_1', .081, .362, .02, 180)
    _place(blocks, 'ipw3_1_2', .105, .362, .03, 180)
    for b in blocks:
        if b['BlockType'] == 'CELL':
            b['Relationships'][0]['Ids'].reverse()
    record = output(blocks)['in_possession'][0]
    assert (record['drug_name'], record['dose'], record['start_date'], record['prescriber_name'] or record['prescriber']) == (
        'Dapagliflozin 10mg tablets', '1 tablet', '15 Oct 2026', 'Ms Stacy Collins')


def test_not_in_possession_table_with_a_dose_column_fills_dose():
    blocks = []
    line(blocks, 'title', 'NOT IN Possession Prescriptions', .17)
    table(blocks, 'nip', [['Drug name', 'Dose', 'Start date', 'End date', 'Days duration', 'Quantity',
                           'Medication notes', 'Prescriber name'],
                          ['Methadone 1mg/ml oral solution', '60ml', '20 Oct 2026', '16 Nov 2026', '28 day(s)',
                           '1680 ml', '', 'Dr Rachel Kavalier']], .2)
    out = output(blocks)
    record = out['not_in_possession'][0]
    assert (record['drug_name'], record['dose'], record['administration_dose_time_description']) == (
        'Methadone 1mg/ml oral solution', '60ml', '')
    assert not out['in_possession']
    assert not any('Missing critical' in reason for reason in out['review_reason'])


def test_not_in_possession_table_with_the_administration_column_keeps_it():
    blocks = []
    line(blocks, 'title', 'NOT IN Possession Prescriptions', .17)
    table(blocks, 'nip', [['Drug name', 'Administration dose / time description', 'Start date', 'End date',
                           'Days duration', 'Quantity', 'Medication notes', 'Prescriber name'],
                          ['Mirtazapine 15mg tablets', '1 tablet at night', '20 Oct 2026', '16 Nov 2026',
                           '28 day(s)', '28 tablet', '', 'Dr Rachel Kavalier']], .2)
    out = output(blocks)
    record = out['not_in_possession'][0]
    assert (record['dose'], record['administration_dose_time_description']) == ('', '1 tablet at night')
    assert not any('Missing critical' in reason for reason in out['review_reason'])
    # A not-in-possession row missing its dose is still flagged.
    blocks = []
    line(blocks, 'title', 'NOT IN Possession Prescriptions', .17)
    table(blocks, 'nip', [['Drug name', 'Dose', 'Start date', 'End date', 'Days duration', 'Quantity',
                           'Medication notes', 'Prescriber name'],
                          ['Methadone 1mg/ml oral solution', '', '20 Oct 2026', '16 Nov 2026', '28 day(s)',
                           '1680 ml', '', 'Dr Rachel Kavalier']], .2)
    assert any('dose' in reason for reason in output(blocks)['review_reason'])
