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

    def fake_extract(pdf_path, prescription_number, job_id):
        calls.append(pdf_path.name)
        return {
            "prescription_id": f"{job_id}-{prescription_number:03d}",
            "extraction_metadata": {"status": "EXTRACTED", "review_reasons": []},
        }

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
    assert (tmp_path / job_id / "processed" / "uploaded_prescription_001" / "structured_extraction.json").is_file()


def test_extraction_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGCARE_TEXTRACT_STAGING_BUCKET", raising=False)
    monkeypatch.delenv("SIGCARE_S3_BUCKET", raising=False)
    pdf = tmp_path / "p.pdf"
    _make_pdf(pdf, [("Patient name: A", "Page 1 of 1")])
    result, status, error = processing.extract_one(pdf, 1, "b" * 32)
    assert status == "ERROR"
    assert "SIGCARE_TEXTRACT_STAGING_BUCKET" in error
