"""Single-file FastAPI wrapper for Sigcare local testing.

Workflow:
PDF upload -> PDF integrity check -> front-sheet detection and removal ->
prescription splitting -> quality assessment and OCR-oriented image cleanup for
each split prescription -> AWS Textract structured extraction -> JSON report/artifacts.

Processing runs in a background thread. POST returns 202 with a job_id at once;
poll GET /api/v1/quality-check/{job_id} until status is COMPLETED or FAILED.

Run from the repository root after installing dependencies:
    uvicorn sigcare_quality.api:app --host 127.0.0.1 --port 8000

This is for local/internal testing. Do not expose it publicly without authentication,
access controls, and an approved retention policy for confidential prescription data.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import fitz
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
import pytesseract

from .processing import JobStatus, process_document
from .textract_integration import readiness_checks

APP_NAME = "Sigcare Document Quality API"
MAX_UPLOAD_BYTES = int(os.getenv("SIGCARE_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
OUTPUT_ROOT = Path(os.getenv("SIGCARE_API_OUTPUT_DIR", "api_outputs")).resolve()
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
# Whole jobs run one after another by default; each job is CPU heavy (Tesseract, OpenCV).
JOB_WORKERS = int(os.getenv("SIGCARE_JOB_WORKERS", "1"))

logging.basicConfig(
    level=os.getenv("SIGCARE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("sigcare_quality.api")

JOB_EXECUTOR = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="sigcare-job")

app = FastAPI(
    title=APP_NAME,
    version="0.5.0",
    description=(
        "Local test API: remove front sheets, split a combined prescription PDF, "
        "assess image quality, create OCR-oriented cleaned page images and extract "
        "structured prescription data with AWS Textract. PASS is not proof of authenticity."
    ),
)


def _process_job(
    job_id: str,
    source_file_name: str,
    dpi: int,
    create_cleanup: bool,
    status: JobStatus,
) -> None:
    job_dir = OUTPUT_ROOT / job_id
    try:
        process_document(
            input_pdf=job_dir / "input" / "uploaded.pdf",
            job_dir=job_dir,
            job_id=job_id,
            source_file_name=source_file_name,
            status=status,
            dpi=dpi,
            create_cleanup=create_cleanup,
            path_base=OUTPUT_ROOT,
        )
    except Exception as exc:
        # Keep the job directory so partial outputs can be inspected.
        LOG.exception("[%s] processing failed", job_id)
        status.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")


# --------------------------- API helpers ---------------------------

def _job_path(job_id: str) -> Path:
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id.")
    path = (OUTPUT_ROOT / job_id).resolve()
    if OUTPUT_ROOT not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid job_id.")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Could not read saved JSON results.") from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Saved results have an unexpected format.")
    return result


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": APP_NAME}


@app.get("/ready")
def ready() -> JSONResponse:
    """Check AWS credentials, the Textract staging bucket and Tesseract."""
    checks = readiness_checks()
    try:
        checks["tesseract"] = {"ok": True, "detail": str(pytesseract.get_tesseract_version())}
    except Exception as exc:
        checks["tesseract"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    ok = all(check["ok"] for check in checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks},
    )


@app.post("/api/v1/quality-check", status_code=202)
async def quality_check(
    file: UploadFile = File(...),
    dpi: int = Query(default=220, ge=100, le=400),
    create_cleanup: bool = True,
) -> dict[str, Any]:
    """Upload a combined PDF and start processing; poll the returned status_url."""
    if not file.filename or Path(file.filename).suffix.lower() != ".pdf":
        await file.close()
        raise HTTPException(status_code=400, detail="Upload a file with a .pdf extension.")

    job_id = uuid.uuid4().hex
    job_dir = OUTPUT_ROOT / job_id
    input_dir = job_dir / "input"
    job_dir.mkdir(parents=True, exist_ok=False)
    input_dir.mkdir()
    input_pdf = input_dir / "uploaded.pdf"
    total_bytes = 0

    try:
        with input_pdf.open("wb") as destination:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"PDF exceeds upload limit ({MAX_UPLOAD_BYTES} bytes).",
                    )
                destination.write(chunk)

        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        with input_pdf.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise HTTPException(status_code=400, detail="The uploaded file is not a valid PDF.")

        # Integrity gate: verify PDF can be opened before queuing.
        try:
            with fitz.open(input_pdf) as doc:
                if doc.needs_pass:
                    raise HTTPException(status_code=400, detail="Password-protected PDFs are not supported.")
                source_page_count = doc.page_count
                if source_page_count < 1:
                    raise HTTPException(status_code=400, detail="The PDF contains no pages.")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail="The PDF could not be opened or parsed.") from exc
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    source_file_name = Path(file.filename).name
    status = JobStatus(job_dir, job_id, source_file_name)
    JOB_EXECUTOR.submit(_process_job, job_id, source_file_name, dpi, create_cleanup, status)
    LOG.info("[%s] queued %s (%d pages)", job_id, source_file_name, source_page_count)

    return {
        "job_id": job_id,
        "status": "QUEUED",
        "source_file_name": source_file_name,
        "source_page_count": source_page_count,
        "status_url": f"/api/v1/quality-check/{job_id}",
    }


@app.get("/api/v1/quality-check/{job_id}")
def get_quality_result(job_id: str) -> dict[str, Any]:
    """Return the full result when finished, otherwise the current job status."""
    job_dir = _job_path(job_id)
    result_path = job_dir / "result.json"
    if result_path.is_file():
        return _load_json(result_path)
    status_path = job_dir / "status.json"
    if status_path.is_file():
        return _load_json(status_path)
    raise HTTPException(status_code=404, detail="Job not found.")


@app.get("/api/v1/quality-check/{job_id}/artifacts")
def list_artifacts(job_id: str) -> dict[str, Any]:
    job_dir = _job_path(job_id)
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job not found.")
    items = []
    for path in job_dir.rglob("*"):
        if path.is_file() and path.parent.name != "input":
            relative = path.relative_to(job_dir).as_posix()
            items.append({
                "path": path.relative_to(OUTPUT_ROOT).as_posix(),
                "download_url": f"/api/v1/quality-check/{job_id}/artifact/{relative}",
            })
    return {"job_id": job_id, "artifacts": items}


@app.get("/api/v1/quality-check/{job_id}/artifact/{relative_path:path}")
def download_artifact(job_id: str, relative_path: str) -> FileResponse:
    job_dir = _job_path(job_id)
    candidate = (job_dir / relative_path).resolve()
    if job_dir not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found.")
    if candidate.parent == job_dir / "input":
        raise HTTPException(status_code=403, detail="Downloading source PDFs is disabled.")
    return FileResponse(candidate, filename=candidate.name)
