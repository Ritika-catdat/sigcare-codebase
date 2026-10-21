"""Single-file FastAPI wrapper for Sigcare local testing.

Workflow:
PDF upload -> PDF integrity check -> prescription splitting -> quality assessment
and OCR-oriented image cleanup for each split prescription -> JSON report/artifacts.

Run from the repository root after installing dependencies:
    uvicorn sigcare_quality.api:app --host 127.0.0.1 --port 8000

This is for local/internal testing. Do not expose it publicly without authentication,
access controls, and an approved retention policy for confidential prescription data.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import fitz
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from PIL import Image
import pytesseract

# Keep using the user's existing quality + cleanup implementation.
from .pipeline import run_quality_pipeline

APP_NAME = "Sigcare Document Quality API"
MAX_UPLOAD_BYTES = int(os.getenv("SIGCARE_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
OUTPUT_ROOT = Path(os.getenv("SIGCARE_API_OUTPUT_DIR", "api_outputs")).resolve()
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title=APP_NAME,
    version="0.4.0",
    description=(
        "Local test API: split a combined prescription PDF, assess image quality, "
        "and create OCR-oriented cleaned page images. PASS is not proof of authenticity."
    ),
)


# ------------------------- Splitting logic -------------------------

def _find_page_marker(text: str) -> tuple[int | None, int | None]:
    """Find counters such as 'Page 1 of 2', '1 of 2', or 'Page 1/2'."""
    text = text.lower().replace("|", "1")
    pattern = r"(?:page\s*)?(\d{1,3})\s*(?:of|/)\s*(\d{1,3})"
    for match in reversed(list(re.finditer(pattern, text))):
        current, total = int(match.group(1)), int(match.group(2))
        if 1 <= current <= total <= 200:
            return current, total
    return None, None


def _footer_text(page: fitz.Page) -> str:
    rect = page.rect
    footer = fitz.Rect(
        rect.x0,
        rect.y0 + rect.height * 0.78,
        rect.x1,
        rect.y1,
    )
    return page.get_text("text", clip=footer, sort=True)


def _ocr_page_for_marker(page: fitz.Page) -> str:
    pix = page.get_pixmap(dpi=250, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if image.width < 1600:
        scale = 1600 / image.width
        image = image.resize((int(image.width * scale), int(image.height * scale)))

    for orientation in (
        image,
        image.transpose(Image.Transpose.ROTATE_90),
        image.transpose(Image.Transpose.ROTATE_180),
        image.transpose(Image.Transpose.ROTATE_270),
    ):
        text = pytesseract.image_to_string(orientation, config="--psm 11")
        if _find_page_marker(text)[0] is not None:
            return text
    return pytesseract.image_to_string(image, config="--psm 11")


def _get_marker(page: fitz.Page) -> tuple[int | None, int | None]:
    # Prefer the footer, then the selectable text layer, then OCR.
    text = _footer_text(page)
    marker = _find_page_marker(text)
    if marker[0] is not None:
        return marker

    marker = _find_page_marker(page.get_text("text", sort=True))
    if marker[0] is not None:
        return marker

    try:
        return _find_page_marker(_ocr_page_for_marker(page))
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError(
            "Tesseract OCR is required because page counters were not found in PDF text. "
            "Install the Tesseract system application and the pytesseract Python package."
        ) from exc


def _group_pages(markers: list[tuple[int | None, int | None]]) -> list[list[int]]:
    """Group pages by page-of-total counters; indices are zero-based."""
    groups: list[list[int]] = []
    i = 0
    count = len(markers)

    while i < count:
        current, total = markers[i]
        if current == 1 and total is not None and total > 1:
            group = [i]
            expected = 2
            j = i + 1
            while j < count and expected <= total:
                next_current, next_total = markers[j]
                if next_current == 1:
                    break
                if next_current is not None and (
                    next_current != expected or next_total != total
                ):
                    break
                # If the next page has no counter, keep it as a likely continuation.
                group.append(j)
                expected += 1
                j += 1
            groups.append(group)
            i = j
        else:
            # Unmarked pages remain single-page prescriptions unless consumed as
            # an expected continuation by the preceding group.
            groups.append([i])
            i += 1

    return groups


def _split_pdf(input_pdf: Path, split_dir: Path) -> tuple[list[Path], list[str]]:
    """Split into separate PDFs and write split_report.txt."""
    split_dir.mkdir(parents=True, exist_ok=True)
    split_report: list[str] = []
    outputs: list[Path] = []

    with fitz.open(input_pdf) as source:
        if source.needs_pass:
            raise ValueError("Password-protected PDFs are not supported.")
        if source.page_count == 0:
            raise ValueError("The PDF contains no pages.")

        markers: list[tuple[int | None, int | None]] = []
        for page_index, page in enumerate(source):
            marker = _get_marker(page)
            markers.append(marker)
            if marker[0] is None:
                split_report.append(f"Source page {page_index + 1}: No marker detected")
            else:
                split_report.append(
                    f"Source page {page_index + 1}: Page {marker[0]} of {marker[1]}"
                )

        groups = _group_pages(markers)
        for number, indices in enumerate(groups, start=1):
            target = fitz.open()
            try:
                for index in indices:
                    target.insert_pdf(source, from_page=index, to_page=index)
                target_path = split_dir / f"{input_pdf.stem}_prescription_{number:03d}.pdf"
                target.save(target_path, garbage=4, deflate=True)
                outputs.append(target_path)
            finally:
                target.close()
            split_report.append(
                f"Output {number}: source pages " + ", ".join(str(i + 1) for i in indices)
            )

    (split_dir / "split_report.txt").write_text("\n".join(split_report), encoding="utf-8")
    return outputs, split_report


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


@app.post("/api/v1/quality-check")
async def quality_check(
    file: UploadFile = File(...),
    dpi: int = Query(default=220, ge=100, le=400),
    create_cleanup: bool = True,
) -> dict[str, Any]:
    """Upload a combined PDF; split first, then assess and clean each prescription."""
    if not file.filename or Path(file.filename).suffix.lower() != ".pdf":
        await file.close()
        raise HTTPException(status_code=400, detail="Upload a file with a .pdf extension.")

    job_id = uuid.uuid4().hex
    job_dir = OUTPUT_ROOT / job_id
    input_dir = job_dir / "input"
    split_dir = job_dir / "split_prescriptions"
    processed_dir = job_dir / "processed"
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

        # Integrity gate: verify PDF can be opened before splitting.
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

        # Stage 1: split the original PDF.
        try:
            split_files, split_report = _split_pdf(input_pdf, split_dir)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Prescription splitting failed ({type(exc).__name__}). Check server logs.",
            ) from exc

        if not split_files:
            raise HTTPException(status_code=500, detail="No prescription files were created.")

        # Stage 2: existing quality assessment and cleanup pipeline for each split.
        results: list[dict[str, Any]] = []
        for number, split_file in enumerate(split_files, start=1):
            output_dir = processed_dir / split_file.stem
            report = run_quality_pipeline(
                input_pdf=split_file,
                output_dir=output_dir,
                dpi=dpi,
                create_cleanup=create_cleanup,
            )
            results.append({
                "prescription_number": number,
                "prescription_pdf": split_file.name,
                "status": report.get("overall_status", "UNKNOWN"),
                "report": report,
                "quality_report_json": (output_dir / "quality_report.json").relative_to(OUTPUT_ROOT).as_posix(),
                "quality_report_txt": (output_dir / "quality_report.txt").relative_to(OUTPUT_ROOT).as_posix(),
                "cleaned_pages_directory": (
                    (output_dir / "cleaned_pages").relative_to(OUTPUT_ROOT).as_posix()
                    if create_cleanup else None
                ),
            })

        overall_status = (
            "COMPLETED_WITH_REVIEW"
            if any(r["status"] != "PASS" for r in results)
            else "COMPLETED"
        )
        response = {
            "job_id": job_id,
            "status": overall_status,
            "source_file_name": Path(file.filename).name,
            "source_page_count": source_page_count,
            "prescriptions_created": len(split_files),
            "prescriptions_processed": len(results),
            "split_report": (split_dir / "split_report.txt").relative_to(OUTPUT_ROOT).as_posix(),
            "results": results,
            "note": (
                "Technical screening only; PASS is not proof of authenticity. "
                "Review all flagged pages and compare cleaned images with the unchanged original."
            ),
        }
        (job_dir / "result.json").write_text(json.dumps(response, indent=2), encoding="utf-8")
        return response

    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"Processing failed ({type(exc).__name__}). Check server logs.",
        ) from exc
    finally:
        await file.close()


@app.get("/api/v1/quality-check/{job_id}")
def get_quality_result(job_id: str) -> dict[str, Any]:
    job_dir = _job_path(job_id)
    result_path = job_dir / "result.json"
    if not result_path.is_file():
        raise HTTPException(status_code=404, detail="Job not found or results unavailable.")
    return _load_json(result_path)


@app.get("/api/v1/quality-check/{job_id}/artifacts")
def list_artifacts(job_id: str) -> dict[str, Any]:
    job_dir = _job_path(job_id)
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job not found.")
    items = []
    for path in job_dir.rglob("*"):
        if path.is_file() and path.name != "uploaded.pdf":
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
    if candidate.name == "uploaded.pdf":
        raise HTTPException(status_code=403, detail="Downloading source PDFs is disabled.")
    return FileResponse(candidate, filename=candidate.name)
