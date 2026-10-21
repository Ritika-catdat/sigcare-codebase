"""FastAPI wrapper for the existing Sigcare document-quality pipeline.

Run from the repository root:
    uvicorn sigcare_quality.api:app --host 0.0.0.0 --port 8000

By default, completed job outputs are stored in ./api_outputs.
Set SIGCARE_API_OUTPUT_DIR to change that location.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .pipeline import run_quality_pipeline

APP_NAME = "Sigcare Document Quality API"
MAX_UPLOAD_BYTES = int(os.getenv("SIGCARE_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
OUTPUT_ROOT = Path(os.getenv("SIGCARE_API_OUTPUT_DIR", "api_outputs")).resolve()
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title=APP_NAME,
    version="0.1.0",
    description=(
        "API wrapper around the existing Sigcare document-quality and image-cleanup "
        "pipeline. Technical screening results are not proof of document authenticity."
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    """Simple liveness endpoint."""
    return {"status": "ok", "service": APP_NAME}


@app.post("/api/v1/quality-check")
async def quality_check(
    file: UploadFile = File(...),
    dpi: int = 220,
    create_cleanup: bool = True,
) -> JSONResponse:
    """Accept one PDF, run the current pipeline, and return its JSON report."""
    if not file.filename or Path(file.filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Upload a file with a .pdf extension.")

    if not 100 <= dpi <= 400:
        raise HTTPException(status_code=422, detail="dpi must be between 100 and 400.")

    job_id = uuid.uuid4().hex
    job_dir = OUTPUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    input_path = job_dir / "input.pdf"
    output_dir = job_dir / "output"
    bytes_written = 0

    try:
        with input_path.open("wb") as destination:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"PDF exceeds the {MAX_UPLOAD_BYTES}-byte upload limit.",
                    )
                destination.write(chunk)

        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")

        with input_path.open("rb") as uploaded:
            if not uploaded.read(5).startswith(b"%PDF-"):
                raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF.")

        report: dict[str, Any] = run_quality_pipeline(
            input_pdf=input_path,
            output_dir=output_dir,
            dpi=dpi,
            create_cleanup=create_cleanup,
        )

        report_path = output_dir / "quality_report.json"
        if not report_path.exists():
            raise HTTPException(
                status_code=500,
                detail="The pipeline finished without creating quality_report.json.",
            )

        return JSONResponse(
            status_code=200,
            content={
                "job_id": job_id,
                "status": report.get("overall_status", "UNKNOWN"),
                "report": report,
                "artifacts": {
                    "quality_report_json": str((output_dir / "quality_report.json").relative_to(OUTPUT_ROOT)),
                    "quality_report_txt": str((output_dir / "quality_report.txt").relative_to(OUTPUT_ROOT)),
                    "cleaned_pages_directory": (
                        str((output_dir / "cleaned_pages").relative_to(OUTPUT_ROOT))
                        if create_cleanup else None
                    ),
                },
                "note": (
                    "Technical screening only; PASS is not proof of authenticity. "
                    "Review findings against the unchanged original document."
                ),
            },
        )
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"Document processing failed ({type(exc).__name__}). Check server logs.",
        ) from exc
    finally:
        await file.close()


@app.get("/api/v1/quality-check/{job_id}")
def get_quality_result(job_id: str) -> dict[str, Any]:
    """Fetch the saved JSON report for a previously processed job."""
    if not job_id.isalnum() or len(job_id) != 32:
        raise HTTPException(status_code=400, detail="Invalid job_id.")

    report_path = OUTPUT_ROOT / job_id / "output" / "quality_report.json"
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="No quality report found for this job_id.")

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Could not read the saved quality report.") from exc

    return {
        "job_id": job_id,
        "status": report.get("overall_status", "UNKNOWN"),
        "report": report,
        "note": "Technical screening only; PASS is not proof of authenticity.",
    }
