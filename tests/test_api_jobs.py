import json

import fitz
import pytest

from sigcare_quality import api, processing
from sigcare_quality.frontsheet import classify_frontsheet

FRONT_SHEET_TEXT = (
    "HMP Winchester\nSigma front sheet\nPatient name\nWing\nItems ordered\n"
    "MISSING(M)\nON-TO-FOLLOW\nEMAIL(E)\nAttached to each order sent"
)


def _make_pdf(path, pages):
    """pages: list of (body_text, footer_text or None)."""
    doc = fitz.open()
    for body, footer in pages:
        page = doc.new_page()
        page.insert_text((72, 100), body, fontsize=16)
        if footer:
            page.insert_text((72, page.rect.height - 40), footer, fontsize=10)
    doc.save(path)
    doc.close()


def test_classify_frontsheet():
    assert classify_frontsheet(FRONT_SHEET_TEXT)["likely_frontsheet"] is True
    assert classify_frontsheet("Patient name: Mr A\nDrug name Dose")["likely_frontsheet"] is False


def test_remove_pages_and_split_keep_original_page_numbers(tmp_path):
    source = tmp_path / "in.pdf"
    _make_pdf(source, [
        ("front sheet", None),
        ("Patient name: A", "Page 1 of 2"),
        ("Patient name: A", "Page 2 of 2"),
        ("front sheet", None),
        ("Patient name: B", "Page 1 of 1"),
    ])
    filtered = tmp_path / "filtered.pdf"
    kept = processing.remove_pages(source, filtered, {1, 4})
    assert kept == [2, 3, 5]

    files, pages, report = processing.split_pdf(
        filtered, tmp_path / "split", original_pages=kept, stem="uploaded"
    )
    assert [f.name for f in files] == ["uploaded_prescription_001.pdf", "uploaded_prescription_002.pdf"]
    assert pages == [[2, 3], [5]]
    assert "Output 1: source pages 2, 3" in report


def test_process_job_removes_front_sheets_and_extracts(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "OUTPUT_ROOT", tmp_path)
    calls = []

    def fake_extract(pdf_path, prescription_number, job_id, original_pdf_location, prescription_location):
        calls.append(pdf_path.name)
        return {"flag_for_review": False, "review_reason": [], "file_name": pdf_path.name,
                "prescription_number": str(prescription_number),
                "original_pdf_location": original_pdf_location,
                "prescription_location": prescription_location}

    monkeypatch.setattr(processing, "extract_prescription_pdf", fake_extract)

    job_id = "a" * 32
    (tmp_path / job_id / "input").mkdir(parents=True)
    _make_pdf(tmp_path / job_id / "input" / "uploaded.pdf", [
        (FRONT_SHEET_TEXT, None),
        ("Patient name: A", "Page 1 of 2"),
        ("Patient name: A", "Page 2 of 2"),
        ("Patient name: B", "Page 1 of 1"),
    ])
    status = processing.JobStatus(tmp_path / job_id, job_id, "test.pdf")

    api._process_job(job_id, "test.pdf", 120, False, status)

    status_data = json.loads((tmp_path / job_id / "status.json").read_text(encoding="utf-8"))
    assert status_data["error"] is None, status_data["error"]
    result = json.loads((tmp_path / job_id / "result.json").read_text(encoding="utf-8"))
    assert result["frontsheet_detection"]["removed_pages"] == [1]
    assert result["frontsheet_detection"]["removal_performed"] is True
    assert [r["source_pages"] for r in result["results"]] == [[2, 3], [4]]
    assert [r["extraction_status"] for r in result["results"]] == ["EXTRACTED", "EXTRACTED"]
    assert sorted(calls) == ["uploaded_prescription_001.pdf", "uploaded_prescription_002.pdf"]
    assert (tmp_path / job_id / "processed" / "uploaded_prescription_001" / "prescription.json").is_file()
    second = result["results"][1]["prescription"]
    assert second["prescription_number"] == "2"
    assert second["original_pdf_location"] == f"{job_id}/input/uploaded.pdf"
    assert second["prescription_location"] == f"{job_id}/split_prescriptions/uploaded_prescription_002.pdf"


def test_extraction_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGCARE_TEXTRACT_STAGING_BUCKET", raising=False)
    monkeypatch.delenv("SIGCARE_S3_BUCKET", raising=False)
    pdf = tmp_path / "p.pdf"
    _make_pdf(pdf, [("Patient name: A", "Page 1 of 1")])
    result, status, error = processing.extract_one(pdf, 1, "b" * 32, "s3://b/orig.pdf", "s3://b/p.pdf")
    assert status == "ERROR"
    assert "SIGCARE_TEXTRACT_STAGING_BUCKET" in error
    # Same contract as a successful extraction, flagged with the reason.
    assert result["flag_for_review"] is True and error in result["review_reason"]
    assert (result["file_name"], result["prescription_number"]) == ("p.pdf", "1")
    assert result["prescription_location"] == "s3://b/p.pdf"


def test_oversized_pages_are_rendered_within_the_pixel_cap():
    from sigcare_quality.integrity import MAX_RENDER_PX, page_image
    doc = fitz.open()
    doc.new_page(width=5000, height=7000)   # ~70 x 97 inches
    doc.new_page()                           # A4-ish 595 x 842 points
    huge, normal = doc[0], doc[1]
    assert max(page_image(huge, 250).size) <= MAX_RENDER_PX
    # Normal pages keep their requested resolution.
    assert max(page_image(normal, 250).size) == round(842 * 250 / 72)
    doc.close()


def test_frontsheet_ocr_timeout_marks_page_for_review(tmp_path, monkeypatch):
    from sigcare_quality import frontsheet
    pdf = tmp_path / "in.pdf"
    _make_pdf(pdf, [("page one", None), ("page two", None), ("page three", None)])
    calls = iter(["text one", RuntimeError("Tesseract process timeout"), FRONT_SHEET_TEXT])

    def fake_ocr(image):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(frontsheet, "ocr_image", fake_ocr)
    monkeypatch.setattr(frontsheet, "OCR_WORKERS", 1)  # deterministic call order
    results = frontsheet.detect_frontsheets(pdf, tmp_path / "report.json")
    assert [r["source_page"] for r in results] == [1, 2, 3]
    assert results[1]["manual_review_required"] and "timeout" in results[1]["error"]
    assert results[2]["likely_frontsheet"] is True


def test_blank_pages_are_removed_before_splitting(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(processing, "extract_prescription_pdf",
                        lambda pdf_path, prescription_number, job_id, original_pdf_location, prescription_location:
                        {"flag_for_review": False, "review_reason": []})
    job_id = "c" * 32
    (tmp_path / job_id / "input").mkdir(parents=True)
    pdf = tmp_path / job_id / "input" / "uploaded.pdf"
    doc = fitz.open()
    for body, footer in [("Patient name: A", "Page 1 of 1"), (None, None), ("Patient name: B", "Page 1 of 1")]:
        page = doc.new_page()
        if body:
            # Enough printed text to count as a real page.
            for i in range(25):
                page.insert_text((72, 100 + i * 24), f"{body} Drug name Paracetamol tablets line {i}", fontsize=14)
            page.insert_text((72, page.rect.height - 40), footer, fontsize=10)
    doc.save(pdf)
    doc.close()
    status = processing.JobStatus(tmp_path / job_id, job_id, "test.pdf")

    api._process_job(job_id, "test.pdf", 120, False, status)

    result = json.loads((tmp_path / job_id / "result.json").read_text(encoding="utf-8"))
    assert result["frontsheet_detection"]["blank_pages"] == [2]
    assert result["frontsheet_detection"]["removed_pages"] == [2]
    assert [r["source_pages"] for r in result["results"]] == [[1], [3]]


def test_page_with_little_ink_but_readable_words_is_not_blank():
    from sigcare_quality.frontsheet import is_blank
    assert is_blank("", 0.0005) is True
    assert is_blank("~ . ,", 0.001) is True                         # speckle / bleed-through noise
    assert is_blank("Please dispense 7 days", 0.001) is False      # a short note on its own page
    assert is_blank("", 0.05) is False                              # ink that OCR could not read
