from __future__ import annotations

import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from prescription_ocr.config import Config
from prescription_ocr.document_parser import Document
from prescription_ocr.output_contract import build_output
from prescription_ocr.prescription_extractor import extract
from prescription_ocr.textract_ocr import TextractOCR

LOG = logging.getLogger(__name__)

# Adaptive retries absorb Textract/S3 throttling when several prescriptions run
# in parallel; explicit timeouts stop a missing network route from hanging a job.
_BOTO_CONFIG = BotoConfig(
    retries={"mode": "adaptive", "max_attempts": 10},
    connect_timeout=5,
    read_timeout=60,
)
_clients: dict[str, Any] = {}
_clients_lock = threading.Lock()


def _region() -> str:
    return os.getenv("AWS_REGION", "eu-west-2")


def _client(service: str) -> Any:
    # boto3 clients are thread-safe; create each one once and share it.
    with _clients_lock:
        if service not in _clients:
            _clients[service] = boto3.client(service, region_name=_region(), config=_BOTO_CONFIG)
        return _clients[service]


def _staging_bucket() -> str:
    # A dedicated staging bucket is optional; the main Sigcare bucket is used otherwise.
    bucket = (
        os.getenv("SIGCARE_TEXTRACT_STAGING_BUCKET", "").strip()
        or os.getenv("SIGCARE_S3_BUCKET", "").strip()
    )
    if not bucket or bucket.startswith("your-"):
        raise RuntimeError(
            "SIGCARE_TEXTRACT_STAGING_BUCKET (or SIGCARE_S3_BUCKET) is not configured "
            "(set it in .env to the real S3 bucket name)."
        )
    return bucket


def prescription_source(
    pdf_path: Path,
    prescription_number: int,
    job_id: str,
    original_pdf_location: str = "",
    prescription_location: str = "",
) -> dict[str, Any]:
    """Source metadata for the downstream JSON contract (output_contract.build_output)."""
    return {
        "prescription_id": f"{job_id}-{prescription_number:03d}",
        "key": pdf_path.name,
        "prescription_number": prescription_number,
        "original_pdf_location": original_pdf_location,
        "prescription_location": prescription_location,
    }


def extract_prescription_pdf(
    pdf_path: Path,
    prescription_number: int,
    job_id: str,
    original_pdf_location: str = "",
    prescription_location: str = "",
) -> dict[str, Any]:
    """Extract one prescription into the downstream JSON contract.

    Uses a temporary private S3 object that is deleted in a finally block.
    Block/word ids and bounding boxes stay internal; only the contract is returned.
    """
    bucket = _staging_bucket()

    if not pdf_path.is_file():
        raise FileNotFoundError(f"Prescription PDF not found: {pdf_path.name}")

    # Use an unpredictable key and a dedicated staging prefix.
    key = f"sigcare-temporary/{job_id}/{uuid.uuid4().hex}.pdf"

    s3 = _client("s3")
    config = Config(region=_region())
    ocr = TextractOCR(config.region, client=_client("textract"))

    uploaded = False

    try:
        # Upload only the individual split prescription, not the combined PDF.
        s3.upload_file(
            str(pdf_path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": "application/pdf",
                "ServerSideEncryption": "AES256",
            },
        )
        uploaded = True

        response = ocr.analyze_s3(
            bucket=bucket,
            key=key,
            poll_seconds=config.poll_seconds,
            timeout_seconds=config.timeout_seconds,
        )

        source = prescription_source(pdf_path, prescription_number, job_id,
                                     original_pdf_location, prescription_location)

        # Do not persist raw Textract blocks or the source PDF to an output bucket.
        internal = extract(response, source, config)
        return build_output(internal, Document(response), internal["extraction_metadata"]["handwriting"],
                            source, bucket, key)

    finally:
        if uploaded:
            # Attempt cleanup on success and failure. A failed delete is logged
            # but must not discard the extraction; the bucket's lifecycle policy
            # is the safeguard for leftover staging objects.
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:
                LOG.exception("Failed to delete temporary Textract staging object %s.", key)


def readiness_checks() -> dict[str, dict[str, Any]]:
    """Quick AWS checks so credential or bucket problems show up in seconds."""
    quick = BotoConfig(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    checks: dict[str, dict[str, Any]] = {}

    try:
        identity = boto3.client("sts", region_name=_region(), config=quick).get_caller_identity()
        checks["aws_credentials"] = {"ok": True, "detail": identity.get("Arn")}
    except Exception as exc:
        checks["aws_credentials"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    try:
        bucket = _staging_bucket()
        boto3.client("s3", region_name=_region(), config=quick).head_bucket(Bucket=bucket)
        checks["textract_staging_bucket"] = {"ok": True, "detail": bucket}
    except Exception as exc:
        checks["textract_staging_bucket"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    return checks
