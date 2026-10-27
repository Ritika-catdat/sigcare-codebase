import io
import json
from datetime import date

import fitz
import pytest
from botocore.exceptions import ClientError

from sigcare_quality import processing
from sigcare_quality.s3_watcher import S3Watcher, WatcherConfig, date_prefix


class FakeS3:
    """Just enough of the boto3 S3 client for the watcher."""

    def __init__(self):
        self.objects = {}

    def list_objects_v2(self, Bucket, Prefix, **_):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[Key] = Body

    def copy_object(self, Bucket, Key, CopySource, **_):
        self.objects[Key] = self.objects[CopySource["Key"]]

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def download_file(self, Bucket, Key, Filename):
        with open(Filename, "wb") as handle:
            handle.write(self.objects[Key])

    def upload_file(self, Filename, Bucket, Key, **_):
        with open(Filename, "rb") as handle:
            self.objects[Key] = handle.read()


def _pdf_bytes():
    doc = fitz.open()
    for footer in ("Page 1 of 2", "Page 2 of 2"):
        page = doc.new_page()
        page.insert_text((72, 100), "Patient name: A", fontsize=16)
        page.insert_text((72, page.rect.height - 40), footer, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    def fake_extract(pdf_path, prescription_number, job_id):
        return {"prescription_id": f"{job_id}-{prescription_number:03d}",
                "extraction_metadata": {"status": "EXTRACTED", "review_reasons": []}}

    monkeypatch.setattr(processing, "extract_prescription_pdf", fake_extract)
    config = WatcherConfig(bucket="test-bucket", work_dir=tmp_path / "work")
    return S3Watcher(config, s3=FakeS3())


def test_date_prefix():
    config = WatcherConfig(bucket="b")
    assert date_prefix(date(2026, 10, 3), config) == "2026/Oct/03"
    assert date_prefix(date(2026, 1, 3), WatcherConfig(bucket="b", root_prefix="sigcare",
                                                        month_case="lower")) == "sigcare/2026/jan/03"


def test_process_key_moves_input_and_writes_outputs(watcher):
    s3 = watcher.s3
    original = _pdf_bytes()
    s3.objects["2026/Oct/27/input/Doc 1.pdf"] = original
    s3.objects["2026/Oct/27/input/"] = b""            # folder placeholder
    s3.objects["2026/Oct/27/input/notes.txt"] = b"x"   # ignored

    assert watcher.pending_inputs(date(2026, 10, 27)) == ["2026/Oct/27/input/Doc 1.pdf"]
    # Synthetic, mostly blank pages trip the quality check, so REVIEW is expected.
    assert watcher.process_key("2026/Oct/27/input/Doc 1.pdf").startswith("COMPLETED")

    keys = set(s3.objects)
    assert "2026/Oct/27/input/Doc 1.pdf" not in keys
    assert s3.objects["2026/Oct/27/processed/Doc 1.pdf"] == original
    # Untouched original kept before cleaning, for later verification.
    assert s3.objects["2026/Oct/27/intermediary/Doc 1/original/Doc 1.pdf"] == original
    assert "2026/Oct/27/intermediary/Doc 1/split_prescriptions/Doc 1_prescription_001.pdf" in keys
    assert "2026/Oct/27/intermediary/Doc 1/processed/Doc 1_prescription_001/structured_extraction.json" in keys
    assert any(k.startswith("2026/Oct/27/intermediary/Doc 1/processed/Doc 1_prescription_001/cleaned_pages/") for k in keys)

    result = json.loads(s3.objects["2026/Oct/27/processed/Doc 1.json"])
    assert result["status"].startswith("COMPLETED")
    assert result["s3"]["original_copy_key"] == "2026/Oct/27/intermediary/Doc 1/original/Doc 1.pdf"
    assert result["results"][0]["source_pages"] == [1, 2]
    status = json.loads(s3.objects["2026/Oct/27/intermediary/Doc 1/status.json"])
    assert status["status"].startswith("COMPLETED") and status["original_sha256"]


def test_failed_file_stays_in_input_and_is_skipped(watcher):
    s3 = watcher.s3
    s3.objects["2026/Oct/27/input/bad.pdf"] = b"%PDF-1.7 not really a pdf"

    assert watcher.process_key("2026/Oct/27/input/bad.pdf") == "FAILED"
    assert "2026/Oct/27/input/bad.pdf" in s3.objects
    status = json.loads(s3.objects["2026/Oct/27/intermediary/bad/status.json"])
    assert status["status"] == "FAILED" and status["error"]
    assert s3.objects["2026/Oct/27/intermediary/bad/original/bad.pdf"] == b"%PDF-1.7 not really a pdf"

    assert watcher.process_key("2026/Oct/27/input/bad.pdf") == "SKIPPED"
