import io
import json
from datetime import date

import fitz
import pytest
from botocore.exceptions import ClientError

from sigcare_quality import processing
from sigcare_quality.s3_watcher import S3Watcher, WatcherConfig, date_prefix, intermediary_path

DAY = "2026/Oct/27"
INTER = f"{DAY}/intermediary/Doc 1"


class FakeS3:
    """Just enough of the boto3 S3 client for the watcher."""

    def __init__(self):
        self.objects = {}

    def list_objects_v2(self, Bucket, Prefix, **_):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                              "HeadObject")
        return {}

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
    def fake_extract(pdf_path, prescription_number, job_id, original_pdf_location, prescription_location):
        return {"flag_for_review": False, "review_reasons": [],
                "original_pdf_location": original_pdf_location,
                "prescription_location": prescription_location}

    monkeypatch.setattr(processing, "extract_prescription_pdf", fake_extract)
    config = WatcherConfig(bucket="test-bucket", work_dir=tmp_path / "work")
    return S3Watcher(config, s3=FakeS3())


def test_date_prefix():
    config = WatcherConfig(bucket="b")
    assert date_prefix(date(2026, 10, 3), config) == "2026/Oct/03"
    assert date_prefix(date(2026, 1, 3), WatcherConfig(bucket="b", root_prefix="sigcare",
                                                        month_case="lower")) == "sigcare/2026/jan/03"


def test_intermediary_layout_matches_vm_worker():
    assert intermediary_path("result.json") == "manifest/result.json"
    assert intermediary_path("split_prescriptions/a_prescription_001.pdf") == "split_prescriptions/a_prescription_001.pdf"
    assert intermediary_path("processed/rx1/cleaned_pages/page_001.png") == "cleanup/rx1/page_001.png"
    assert intermediary_path("processed/rx1/quality_report.json") == "forensics/rx1/quality_report.json"
    assert intermediary_path("processed/rx1/prescription.json") == "extraction/rx1/prescription.json"
    assert intermediary_path("frontsheet_reports/frontsheet_report.json") == "reports/frontsheet_report.json"
    assert intermediary_path("input/Doc.pdf") is None
    assert intermediary_path("status.json") is None


def test_process_key_moves_input_and_writes_outputs(watcher):
    s3 = watcher.s3
    original = _pdf_bytes()
    s3.objects[f"{DAY}/input/Doc 1.pdf"] = original
    s3.objects[f"{DAY}/input/"] = b""            # folder placeholder
    s3.objects[f"{DAY}/input/notes.txt"] = b"x"   # ignored

    assert watcher.pending_inputs(date(2026, 10, 27)) == [f"{DAY}/input/Doc 1.pdf"]
    # Synthetic, mostly blank pages trip the quality check, so REVIEW is expected.
    assert watcher.process_key(f"{DAY}/input/Doc 1.pdf").startswith("COMPLETED")

    keys = set(s3.objects)
    assert f"{DAY}/input/Doc 1.pdf" not in keys
    assert s3.objects[f"{DAY}/processed/Doc 1.pdf"] == original
    # Untouched original kept before cleaning, for later verification.
    assert s3.objects[f"{INTER}/original/Doc 1.pdf"] == original
    assert f"{INTER}/split_prescriptions/Doc 1_prescription_001.pdf" in keys
    assert f"{INTER}/cleanup/Doc 1_prescription_001/page_001.png" in keys
    assert f"{INTER}/forensics/Doc 1_prescription_001/quality_report.json" in keys
    assert f"{INTER}/extraction/Doc 1_prescription_001/prescription.json" in keys
    assert f"{INTER}/reports/frontsheet_report.json" in keys
    assert f"{INTER}/manifest/result.json" in keys

    result = json.loads(s3.objects[f"{DAY}/processed/Doc 1.json"])
    assert result["status"].startswith("COMPLETED")
    assert result["s3"]["original_copy_key"] == f"{INTER}/original/Doc 1.pdf"
    assert result["results"][0]["source_pages"] == [1, 2]
    assert result["results"][0]["cleaned_pages_directory"] == f"{INTER}/cleanup/Doc 1_prescription_001"
    assert result["results"][0]["prescription_json"] == (
        f"{INTER}/extraction/Doc 1_prescription_001/prescription.json")
    prescription = result["results"][0]["prescription"]
    assert prescription["original_pdf_location"] == f"s3://test-bucket/{DAY}/processed/Doc 1.pdf"
    assert prescription["prescription_location"] == (
        f"s3://test-bucket/{INTER}/split_prescriptions/Doc 1_prescription_001.pdf")
    summary = json.loads(s3.objects[f"{INTER}/manifest/job_summary.json"])
    assert summary["source_key"] == f"{DAY}/input/Doc 1.pdf" and summary["original_sha256"]
    status = json.loads(s3.objects[f"{INTER}/manifest/status.json"])
    assert status["status"].startswith("COMPLETED")


def test_failed_file_stays_in_input_and_is_skipped(watcher):
    s3 = watcher.s3
    s3.objects[f"{DAY}/input/bad.pdf"] = b"%PDF-1.7 not really a pdf"

    assert watcher.process_key(f"{DAY}/input/bad.pdf") == "FAILED"
    assert f"{DAY}/input/bad.pdf" in s3.objects
    status = json.loads(s3.objects[f"{DAY}/intermediary/bad/manifest/status.json"])
    assert status["status"] == "FAILED" and status["error"]
    assert s3.objects[f"{DAY}/intermediary/bad/original/bad.pdf"] == b"%PDF-1.7 not really a pdf"

    assert watcher.process_key(f"{DAY}/input/bad.pdf") == "SKIPPED"


def test_duplicate_and_manual_recovery_rules(watcher):
    s3 = watcher.s3
    s3.objects[f"{DAY}/input/dup.pdf"] = b"%PDF-"
    s3.objects[f"{DAY}/processed/dup.pdf"] = b"%PDF-"
    assert watcher.process_key(f"{DAY}/input/dup.pdf") == "DUPLICATE"
    assert f"{DAY}/input/dup.pdf" not in s3.objects

    s3.objects[f"{DAY}/input/stuck.pdf"] = b"%PDF-"
    s3.objects[f"{DAY}/intermediary/stuck/manifest/job_summary.json"] = b"{}"
    assert watcher.process_key(f"{DAY}/input/stuck.pdf") == "SKIPPED"
    assert f"{DAY}/input/stuck.pdf" in s3.objects


def test_dry_run_only_lists(tmp_path, monkeypatch):
    monkeypatch.setattr(S3Watcher, "days_to_scan", lambda self: [date(2026, 10, 27)])
    watcher = S3Watcher(WatcherConfig(bucket="b", work_dir=tmp_path, dry_run=True), s3=FakeS3())
    watcher.s3.objects[f"{DAY}/input/a.pdf"] = b"%PDF-"
    assert watcher.scan_once() == {f"{DAY}/input/a.pdf": "DRY_RUN"}
    assert f"{DAY}/input/a.pdf" in watcher.s3.objects


def test_config_accepts_vm_worker_env_names(monkeypatch):
    for name in ("SIGCARE_S3_BUCKET", "SIGCARE_WATCH_INTERVAL_SECONDS", "SIGCARE_WATCH_DRY_RUN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("S3_BUCKET_NAME", "sigcare-test-bucket")
    monkeypatch.setenv("S3_POLL_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("S3_WORKER_DRY_RUN", "true")
    config = WatcherConfig.from_env()
    assert (config.bucket, config.interval_seconds, config.dry_run) == ("sigcare-test-bucket", 30, True)
