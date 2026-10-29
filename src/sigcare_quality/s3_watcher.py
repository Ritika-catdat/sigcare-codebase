"""Watch the day's S3 input folder and process new prescription PDFs.

Bucket layout (agreed folder structure; the bucket differs between test and prod):

    <bucket>/[<root prefix>/]<yyyy>/<MMM>/<dd>/input/<file>.pdf
        new files are dropped here, e.g. 2026/Oct/27/input/Doc.pdf
    .../<dd>/intermediary/<file stem>/
        original/<file>.pdf                unchanged copy saved before any cleaning
        split_prescriptions/               split PDFs + split_report.txt
        cleanup/<prescription>/            cleaned page images used for OCR
        forensics/<prescription>/          quality_report.json / .txt
        extraction/<prescription>/         prescription.json (Textract, downstream contract)
        reports/                           frontsheet_report.json, frontsheets_removed.pdf
        manifest/status.json               RUNNING / COMPLETED / FAILED (+ error)
        manifest/result.json               full result (paths point at the S3 keys above)
        manifest/job_summary.json          written last, when the run succeeded
    .../<dd>/processed/<file>.pdf          the input file, moved here once processed
    .../<dd>/processed/<file stem>.json    final structured result

Rules (same as the earlier VM worker):
- input whose processed/<file> already exists is a duplicate and is deleted;
- input with manifest/job_summary.json but still in input/ needs manual recovery
  and is skipped;
- a failed file stays in input/ with manifest/status.json FAILED and is skipped
  from then on. Delete that status.json to retry it.

Run:
    python -m sigcare_quality.s3_watcher            # poll forever
    python -m sigcare_quality.s3_watcher --once     # one scan, then exit
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import signal
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .processing import JobStatus, now_iso, process_document

LOG = logging.getLogger(__name__)

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _env(*names: str, default: str = "") -> str:
    """First non-empty value among names; old VM worker names are accepted too."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


@dataclass(frozen=True)
class WatcherConfig:
    bucket: str
    root_prefix: str = ""
    month_case: str = "title"
    timezone: str = "Europe/London"
    interval_seconds: int = 60
    lookback_days: int = 1
    work_dir: Path = Path("api_outputs/s3_jobs")
    keep_local: bool = False
    dry_run: bool = False
    dpi: int = 220
    region: str = "eu-west-2"

    @classmethod
    def from_env(cls) -> "WatcherConfig":
        bucket = _env("SIGCARE_S3_BUCKET", "S3_BUCKET_NAME")
        if not bucket:
            raise RuntimeError("SIGCARE_S3_BUCKET is not configured (test and prod use different buckets).")
        output_dir = Path(os.getenv("SIGCARE_API_OUTPUT_DIR", "api_outputs"))
        return cls(
            bucket=bucket,
            root_prefix=_env("SIGCARE_S3_ROOT_PREFIX").strip("/"),
            month_case=_env("SIGCARE_S3_MONTH_CASE", default="title").lower(),
            timezone=_env("SIGCARE_S3_TIMEZONE", default="Europe/London"),
            interval_seconds=int(_env("SIGCARE_WATCH_INTERVAL_SECONDS", "S3_POLL_INTERVAL_SECONDS", default="60")),
            lookback_days=int(_env("SIGCARE_WATCH_LOOKBACK_DAYS", default="1")),
            work_dir=Path(_env("SIGCARE_WATCH_WORK_DIR", default=str(output_dir / "s3_jobs"))),
            keep_local=_env("SIGCARE_WATCH_KEEP_LOCAL", default="false").lower() == "true",
            dry_run=_env("SIGCARE_WATCH_DRY_RUN", "S3_WORKER_DRY_RUN", default="false").lower() == "true",
            dpi=int(_env("SIGCARE_WATCH_DPI", default="220")),
            region=_env("AWS_REGION", default="eu-west-2"),
        )


def date_prefix(day: date, config: WatcherConfig) -> str:
    """yyyy/MMM/dd, e.g. '2026/Oct/27' (with the optional root prefix in front)."""
    month = MONTHS[day.month - 1]
    if config.month_case == "upper":
        month = month.upper()
    elif config.month_case == "title":
        month = month.title()
    parts = [config.root_prefix] if config.root_prefix else []
    parts.append(f"{day.year:04d}/{month}/{day.day:02d}")
    return "/".join(parts)


def intermediary_path(relative: str) -> str | None:
    """Map a path inside the local job folder to its place under intermediary/<file>/.

    Returns None for files that are not uploaded (local status, the input copy).
    """
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] == "input" or parts == ("status.json",):
        return None
    if parts == ("result.json",):
        return "manifest/result.json"
    if parts[0] == "split_prescriptions":
        return "/".join(parts)
    if parts[0] == "processed" and len(parts) >= 3:
        prescription, remainder = parts[1], parts[2:]
        if remainder[0] == "cleaned_pages":
            return "/".join(("cleanup", prescription) + remainder[1:])
        if remainder == ("prescription.json",):
            return f"extraction/{prescription}/prescription.json"
        return "/".join(("forensics", prescription) + remainder)
    return f"reports/{parts[-1]}"


@dataclass
class S3Watcher:
    config: WatcherConfig
    s3: Any = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _warned: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.s3 is None:
            self.s3 = boto3.client(
                "s3",
                region_name=self.config.region,
                config=BotoConfig(retries={"mode": "adaptive", "max_attempts": 10},
                                  connect_timeout=5, read_timeout=60),
            )

    # ----------------------------- S3 helpers -----------------------------

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        kwargs = {"Bucket": self.config.bucket, "Prefix": prefix}
        while True:
            page = self.s3.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return keys
            kwargs["ContinuationToken"] = page["NextContinuationToken"]

    def _exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.config.bucket, Key=key)
            return True
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404 or exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def _read_json(self, key: str) -> dict[str, Any] | None:
        try:
            body = self.s3.get_object(Bucket=self.config.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(body)

    def _put_json(self, key: str, value: Any) -> None:
        self.s3.put_object(
            Bucket=self.config.bucket,
            Key=key,
            Body=json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )

    def _copy(self, source_key: str, target_key: str) -> None:
        self.s3.copy_object(
            Bucket=self.config.bucket,
            Key=target_key,
            CopySource={"Bucket": self.config.bucket, "Key": source_key},
            ServerSideEncryption="AES256",
            MetadataDirective="COPY",
        )

    def _upload_job(self, job_dir: Path, intermediary: str) -> int:
        count = 0
        for path in sorted(job_dir.rglob("*")):
            if not path.is_file() or path.suffix == ".tmp":
                continue
            target = intermediary_path(path.relative_to(job_dir).as_posix())
            if target is None:
                continue
            content_type = {
                ".json": "application/json", ".pdf": "application/pdf",
                ".png": "image/png", ".txt": "text/plain",
            }.get(path.suffix.lower(), "application/octet-stream")
            self.s3.upload_file(
                str(path), self.config.bucket, f"{intermediary}{target}",
                ExtraArgs={"ContentType": content_type, "ServerSideEncryption": "AES256"},
            )
            count += 1
        return count

    # ----------------------------- Processing -----------------------------

    def days_to_scan(self) -> list[date]:
        today = datetime.now(ZoneInfo(self.config.timezone)).date()
        # Look back so files dropped just before midnight are still picked up.
        return [today - timedelta(days=n) for n in range(self.config.lookback_days, -1, -1)]

    def pending_inputs(self, day: date) -> list[str]:
        prefix = f"{date_prefix(day, self.config)}/input/"
        pending = []
        for key in self._list_keys(prefix):
            name = key[len(prefix):]
            if not name or "/" in name:
                continue  # folder placeholder or nested object
            if not name.lower().endswith(".pdf"):
                if key not in self._warned:
                    LOG.warning("Skipping non-PDF input %s", key)
                    self._warned.add(key)
                continue
            pending.append(key)
        return sorted(pending)

    def _skip_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            LOG.warning(message, *args)
            self._warned.add(key)

    def process_key(self, input_key: str) -> str:
        """Process one input object; returns its final status."""
        bucket = self.config.bucket
        name = PurePosixPath(input_key).name
        stem = PurePosixPath(name).stem
        day_prefix = input_key.rsplit("/input/", 1)[0]
        intermediary = f"{day_prefix}/intermediary/{stem}/"
        status_key = f"{intermediary}manifest/status.json"
        summary_key = f"{intermediary}manifest/job_summary.json"
        original_key = f"{intermediary}original/{name}"
        processed_key = f"{day_prefix}/processed/{name}"
        result_key = f"{day_prefix}/processed/{stem}.json"

        if self._exists(processed_key):
            LOG.info("Already archived; removing duplicate input %s", input_key)
            self.s3.delete_object(Bucket=bucket, Key=input_key)
            return "DUPLICATE"
        previous = self._read_json(status_key)
        if previous and previous.get("status") == "FAILED":
            self._skip_once(input_key, "Skipping %s: previous run FAILED (%s). Delete %s to retry.",
                            input_key, previous.get("error"), status_key)
            return "SKIPPED"
        if self._exists(summary_key):
            self._skip_once(input_key, "Manifest exists but source is still in input; "
                            "manual recovery is needed: %s", input_key)
            return "SKIPPED"

        job_id = hashlib.sha256(f"{bucket}/{input_key}/{now_iso()}".encode()).hexdigest()[:32]
        job_dir = self.config.work_dir / day_prefix / stem
        shutil.rmtree(job_dir, ignore_errors=True)
        (job_dir / "input").mkdir(parents=True)
        local_pdf = job_dir / "input" / name
        s3_info = {
            "bucket": bucket,
            "input_key": input_key,
            "original_copy_key": original_key,
            "intermediary_prefix": intermediary,
            "processed_key": processed_key,
            "result_key": result_key,
        }
        run_status = {"job_id": job_id, "status": "RUNNING", "started_at": now_iso(),
                      "error": None, "s3": s3_info}
        LOG.info("Processing s3://%s/%s", bucket, input_key)

        try:
            # Save the untouched input before any cleaning so outputs can be
            # verified against the original later.
            self._copy(input_key, original_key)
            self.s3.download_file(bucket, input_key, str(local_pdf))
            run_status["original_sha256"] = hashlib.sha256(local_pdf.read_bytes()).hexdigest()
            self._put_json(status_key, run_status)

            status = JobStatus(job_dir, job_id, name)
            result = process_document(
                input_pdf=local_pdf,
                job_dir=job_dir,
                job_id=job_id,
                source_file_name=name,
                status=status,
                dpi=self.config.dpi,
                split_stem=stem,
                path_base=job_dir,
                # The original lands in processed/ once the run succeeds.
                original_pdf_location=f"s3://{bucket}/{processed_key}",
                artifact_location=lambda rel: f"s3://{bucket}/{intermediary}{intermediary_path(rel)}",
            )
            self._point_result_at_s3(result, intermediary)
            result["s3"] = s3_info
            result["original_sha256"] = run_status["original_sha256"]
            (job_dir / "result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

            uploaded = self._upload_job(job_dir, intermediary)
            self._put_json(result_key, result)
            self._put_json(summary_key, {
                "source_key": input_key,
                "job_id": job_id,
                "status": result["status"],
                "source_page_count": result["source_page_count"],
                "prescriptions_created": result["prescriptions_created"],
                "removed_frontsheet_pages": result["frontsheet_detection"]["likely_frontsheet_pages"],
                "removed_blank_pages": result["frontsheet_detection"]["blank_pages"],
                "artifacts_uploaded": uploaded,
                "original_sha256": run_status["original_sha256"],
                "processed_at_utc": now_iso(),
                "note": result.get("note"),
            })
            # Move the input to processed/ only after every output is stored.
            self._copy(input_key, processed_key)
            self.s3.delete_object(Bucket=bucket, Key=input_key)

            run_status.update(status=result["status"], finished_at=now_iso(),
                              prescriptions=result["prescriptions_created"],
                              artifacts_uploaded=uploaded)
            self._put_json(status_key, run_status)
            LOG.info("Done %s -> %s (%s, %d prescriptions)",
                     input_key, processed_key, result["status"], result["prescriptions_created"])
            return result["status"]

        except Exception as exc:
            LOG.exception("Processing failed for %s; source retained for investigation", input_key)
            run_status.update(status="FAILED", finished_at=now_iso(),
                              error=f"{type(exc).__name__}: {exc}")
            try:
                # Keep whatever was produced for diagnosis.
                self._upload_job(job_dir, intermediary)
                self._put_json(status_key, run_status)
            except Exception:
                LOG.exception("Could not record failure for %s", input_key)
            return "FAILED"
        finally:
            if not self.config.keep_local:
                shutil.rmtree(job_dir, ignore_errors=True)

    @staticmethod
    def _point_result_at_s3(result: dict[str, Any], intermediary: str) -> None:
        """Rewrite local artifact paths in the result to their S3 keys."""
        def key(relative: str | None) -> str | None:
            if not relative:
                return relative
            parts = PurePosixPath(relative).parts
            if parts[0] == "processed" and parts[-1] == "cleaned_pages":
                return f"{intermediary}cleanup/{parts[1]}"
            target = intermediary_path(relative)
            return f"{intermediary}{target}" if target else relative

        for name in ("split_report", "frontsheet_report"):
            result[name] = key(result.get(name))
        for item in result.get("results", []):
            for name in ("quality_report_json", "cleaned_pages_directory", "prescription_json"):
                item[name] = key(item.get(name))

    def scan_once(self) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for day in self.days_to_scan():
            for key in self.pending_inputs(day):
                if self._stop.is_set():
                    return outcomes
                if self.config.dry_run:
                    LOG.info("[DRY RUN] Eligible input PDF: s3://%s/%s", self.config.bucket, key)
                    outcomes[key] = "DRY_RUN"
                    continue
                outcomes[key] = self.process_key(key)
        return outcomes

    def run_forever(self) -> None:
        LOG.info("S3 watcher started; bucket=%s, today=%s, interval=%ss, dry_run=%s",
                 self.config.bucket, date_prefix(self.days_to_scan()[-1], self.config),
                 self.config.interval_seconds, self.config.dry_run)
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                LOG.exception("Could not scan S3 input folders; retrying in %ss",
                              self.config.interval_seconds)
            self._stop.wait(self.config.interval_seconds)
        LOG.info("Watcher stopped.")

    def stop(self, *_: Any) -> None:
        LOG.info("Stop requested; finishing the current file.")
        self._stop.set()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process prescription PDFs from the day's S3 input folder.")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=_env("SIGCARE_LOG_LEVEL", "LOG_LEVEL", default="INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    watcher = S3Watcher(WatcherConfig.from_env())
    signal.signal(signal.SIGTERM, watcher.stop)
    signal.signal(signal.SIGINT, watcher.stop)
    if args.once:
        outcomes = watcher.scan_once()
        LOG.info("Scan finished: %s", outcomes or "no new files")
    else:
        watcher.run_forever()


if __name__ == "__main__":
    main()
