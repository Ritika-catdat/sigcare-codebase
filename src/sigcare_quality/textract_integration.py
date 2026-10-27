from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import boto3

from prescription_ocr.config import Config
from prescription_ocr.prescription_extractor import extract
from prescription_ocr.textract_ocr import TextractOCR


def extract_prescription_pdf(
    pdf_path: Path,
    prescription_number: int,
    job_id: str,
) -> dict[str, Any]:
    """Extract structured prescription data using a temporary private S3 object.

    The temporary PDF is deleted in a finally block. The extracted JSON is
    returned to the API and is not written to an S3 output prefix by this module.
    """
    bucket = os.getenv("SIGCARE_TEXTRACT_STAGING_BUCKET")
    region = os.getenv("AWS_REGION", "eu-west-2")

    if not bucket:
        raise RuntimeError(
            "SIGCARE_TEXTRACT_STAGING_BUCKET is not configured."
        )

    if not pdf_path.is_file():
        raise FileNotFoundError(f"Prescription PDF not found: {pdf_path.name}")

    # Use an unpredictable key and a dedicated staging prefix.
    key = f"sigcare-temporary/{job_id}/{uuid.uuid4().hex}.pdf"

    s3 = boto3.client("s3", region_name=region)
    config = Config(region=region)
    ocr = TextractOCR(region)

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

        source = {
            "prescription_id": f"{job_id}-{prescription_number:03d}",
            "prescription_number": prescription_number,
            "source_file": pdf_path.name,
        }

        # Return structured JSON in the API response.
        # Do not persist raw Textract blocks or the source PDF to an output bucket.
        return extract(response, source, config)

    finally:
        if uploaded:
            # Attempt cleanup on success and failure.
            # If deletion fails, the exception must be logged and the bucket's
            # lifecycle policy should provide an additional cleanup safeguard.
            try:
                s3.delete_object(Bucket=bucket, Key=key)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "Failed to delete temporary Textract staging object."
                )
                raise