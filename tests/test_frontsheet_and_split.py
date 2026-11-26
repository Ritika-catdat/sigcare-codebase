"""Cover sheets seen in real batches, and splitting pages by patient."""
from sigcare_quality.frontsheet import classify_frontsheet, readable_score
from sigcare_quality.processing import _find_page_marker, _group_pages, _patient, _prisoner_numbers


def test_houseblock_header_sheet_is_a_front_sheet():
    text = ('HMP FOSSE WAY - HEADER SHEET Received on Main MAP by: Houseblock Prison Number '
            'Patient Name Start Date Medication To be Delivered On: Tick to confirm receipt')
    assert classify_frontsheet(text)['likely_frontsheet']


def test_fax_record_sheet_is_a_front_sheet():
    text = ('Date Faxed 12/10 Required Date 13/10 Cover Sheet No. 2 RECORD OF PRESCRIPTIONS SENT TO '
            'SIGCARE PHARMACY Name Number Items Total No. of Scripts DISCLAIMER')
    assert classify_frontsheet(text)['likely_frontsheet']


def test_patient_list_is_a_front_sheet():
    text = 'Client ID Patient Drug Possession Type A5710FK Mr Michael Paul Cap In possession - 28 days'
    assert classify_frontsheet(text)['likely_frontsheet']


def test_prescription_is_not_a_front_sheet():
    text = ('HMP Kirkham Patient name: Mr Lee Dunn Date of birth: 03 Feb 1978 Prisoner number: A0445CZ '
            'IN Possession Prescriptions Drug name Dose Script Notes Start date Signed: Date: Page 1 of 1')
    assert not classify_frontsheet(text)['likely_frontsheet']


def test_upside_down_ocr_garbage_is_not_readable():
    assert readable_score('suoydiosaid uoissassod NI to of on SM STA1 peoy') < 4
    assert readable_score('Patient name: Mr Lee Dunn Date of birth Prisoner number Drug name Dose') >= 4


def test_prisoner_numbers_with_misread_digits():
    assert _prisoner_numbers('Patient name: Mr Richard Osborne, ASS95AL') == {'A5595AL'}
    assert _prisoner_numbers('SNOMED Ua1LL HOSPITAL') == set()


def test_page_of_another_patient_never_continues_a_prescription():
    # The second page is another patient although its counter was misread as "Page 2 of 2".
    markers = [(None, None), (2, 2), (1, 1)]
    patients = [{'A6390FD'}, {'A6873FK'}, {'A6428FF'}]
    assert _group_pages(markers, patients) == [[0], [1], [2]]
    # The same patient's next page (one character misread) still joins.
    assert _group_pages([(1, 2), (2, 2)], [{'A6390FD'}, {'A6390FO'}]) == [[0, 1]]
    assert _group_pages([(1, 2), (None, None)], [{'A6390FD'}, set()]) == [[0, 1]]


def test_misread_page_counter_is_read():
    assert _find_page_marker('Patient name: Mrs Jo Tate\nPage lofi') == (1, 1)
    assert _find_page_marker('Page 1 ofl') == (1, 1)
    assert _find_page_marker('Page 2 of 3') == (2, 3)
    assert _find_page_marker('lots of files') == (None, None)


def test_page_of_another_patient_by_name_never_continues_a_prescription():
    # "Page 1 of 2", then a page whose counter OCR could not read and whose prisoner
    # number was not read: only the patient name tells it is someone else.
    first = _patient('Patient name: Mrs Jo Tate\nPrisoner number: A1234BC')
    other = _patient('Patient name: Mrs Kay Morrow\nDrug name Dose')
    assert _group_pages([(1, 2), (None, None)], [first, other]) == [[0], [1]]
    # The same patient's page 2 with the name misread still joins.
    same = _patient('Patient name: Mrs J0 Tate, A1234BC Date of birth: 01 Jan 1980')
    assert _group_pages([(1, 2), (None, None)], [first, same]) == [[0, 1]]
    assert _group_pages([(1, 2), (None, None)], [first, set()]) == [[0, 1]]


def test_sideways_line_on_a_page_with_little_ink_is_read(monkeypatch):
    from PIL import Image
    from sigcare_quality import frontsheet
    reads = iter(['', 'A0707CL Mr Jo Tate Doxepin solution for injection Not in possession'])
    monkeypatch.setattr(frontsheet, 'ocr_image', lambda image: next(reads, ''))
    text = frontsheet.ocr_any_orientation(Image.new('L', (40, 40), 255), ink=0.005)
    assert 'Doxepin' in text and not frontsheet.is_blank(text, 0.005)
    # No ink at all: the blank back of a sheet is not read again.
    reads = iter(['', 'never read'])
    assert frontsheet.ocr_any_orientation(Image.new('L', (40, 40), 255), ink=0.0) == ''
