"""Watch the day's S3 input folder and process new prescription PDFs.

Bucket layout (agreed folder structure; the bucket differs between test and prod):

    <bucket>/[<root prefix>/]<yyyy>/<MMM>/<dd>/input/<file>.pdf
        new files are dropped here
    .../<dd>/intermediary/<file stem>/
        original/<file>.pdf      unchanged copy saved before any cleaning
        status.json              RUNNING / COMPLETED / FAILED (+ error)
        frontsheet_reports/, frontsheets_removed.pdf, split_prescriptions/,
        processed/<prescription>/{cleaned_pages/, quality_report.*, structured_extraction.json},
        result.json
    .../<dd>/processed/<file>.pdf    the input file, moved here once processed
    .../<dd>/processed/<file stem>.json  final structured result

A file that fails stays in input/ with status FAILED in its intermediary folder and
is skipped from then on. Delete intermediary/<file stem>/status.json to retry it.

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
    dpi: int = 220
    region: str = "eu-west-2"

    @classmethod
    def from_env(cls) -> "WatcherConfig":
        bucket = os.getenv("SIGCARE_S3_BUCKET", "").strip() or os.getenv("S3_BUCKET_NAME", "").strip()
        if not bucket:
            raise RuntimeError("SIGCARE_S3_BUCKET is not configured (test and prod use different buckets).")
        output_dir = Path(os.getenv("SIGCARE_API_OUTPUT_DIR", "api_outputs"))
        return cls(
            bucket=bucket,
            root_prefix=os.getenv("SIGCARE_S3_ROOT_PREFIX", "").strip("/"),
            month_case=os.getenv("SIGCARE_S3_MONTH_CASE", "title").lower(),
            timezone=os.getenv("SIGCARE_S3_TIMEZONE", "Europe/London"),
            interval_seconds=int(os.getenv("SIGCARE_WATCH_INTERVAL_SECONDS", "60")),
            lookback_days=int(os.getenv("SIGCARE_WATCH_LOOKBACK_DAYS", "1")),
            work_dir=Path(os.getenv("SIGCARE_WATCH_WORK_DIR", str(output_dir / "s3_jobs"))),
            keep_local=os.getenv("SIGCARE_WATCH_KEEP_LOCAL", "false").lower() == "true",
            dpi=int(os.getenv("SIGCARE_WATCH_DPI", "220")),
            region=os.getenv("AWS_REGION", "eu-west-2"),
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

    def _upload_tree(self, local_dir: Path, prefix: str, skip: set[Path]) -> int:
        count = 0
        for path in sorted(local_dir.rglob("*")):
            if not path.is_file() or path in skip or path.suffix == ".tmp":
                continue
            content_type = {
                ".json": "application/json", ".pdf": "application/pdf",
                ".png": "image/png", ".txt": "text/plain",
            }.get(path.suffix.lower(), "application/octet-stream")
            self.s3.upload_file(
                str(path), self.config.bucket,
                f"{prefix}{path.relative_to(local_dir).as_posix()}",
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

    def process_key(self, input_key: str) -> str:
        """Process one input object; returns its final status."""
        bucket = self.config.bucket
        name = PurePosixPath(input_key).name
        stem = PurePosixPath(name).stem
        day_prefix = input_key.rsplit("/input/", 1)[0]
        intermediary = f"{day_prefix}/intermediary/{stem}/"
        status_key = f"{intermediary}status.json"
        original_key = f"{intermediary}original/{name}"
        processed_key = f"{day_prefix}/processed/{name}"
        result_key = f"{day_prefix}/processed/{stem}.json"

        previous = self._read_json(status_key)
        if previous and previous.get("status") == "FAILED":
            if input_key not in self._warned:
                LOG.warning("Skipping %s: previous run FAILED (%s). Delete %s to retry.",
                            input_key, previous.get("error"), status_key)
                self._warned.add(input_key)
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
            )
            # Artifact paths in result are relative to the intermediary folder.
            result["s3"] = s3_info
            result["original_sha256"] = run_status["original_sha256"]
            (job_dir / "result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

            uploaded = self._upload_tree(job_dir, intermediary, skip={local_pdf, job_dir / "status.json"})
            self._put_json(result_key, result)
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
            LOG.exception("Processing failed for %s", input_key)
            run_status.update(status="FAILED", finished_at=now_iso(),
                              error=f"{type(exc).__name__}: {exc}")
            try:
                # Keep whatever was produced for diagnosis.
                self._upload_tree(job_dir, intermediary, skip={local_pdf, job_dir / "status.json"})
                self._put_json(status_key, run_status)
            except Exception:
                LOG.exception("Could not record failure for %s", input_key)
            return "FAILED"
        finally:
            if not self.config.keep_local:
                shutil.rmtree(job_dir, ignore_errors=True)

    def scan_once(self) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for day in self.days_to_scan():
            for key in self.pending_inputs(day):
                if self._stop.is_set():
                    return outcomes
                outcomes[key] = self.process_key(key)
        return outcomes

    def run_forever(self) -> None:
        LOG.info("Watching s3://%s/%s/input/ every %ss",
                 self.config.bucket, date_prefix(self.days_to_scan()[-1], self.config),
                 self.config.interval_seconds)
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                LOG.exception("Scan failed; retrying in %ss", self.config.interval_seconds)
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
        level=os.getenv("SIGCARE_LOG_LEVEL", "INFO").upper(),
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
