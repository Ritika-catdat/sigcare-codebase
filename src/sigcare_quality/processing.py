"""Document processing shared by the HTTP API and the S3 watcher.

Pipeline: front-sheet detection and removal -> prescription splitting ->
quality assessment and OCR-oriented cleanup -> AWS Textract structured extraction.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import fitz
from PIL import Image
import pytesseract

# Keep using the user's existing quality + cleanup implementation.
from .pipeline import run_quality_pipeline
from .frontsheet import detect_frontsheets, ocr_image
from .integrity import page_image
from .textract_integration import extract_prescription_pdf, prescription_source
from prescription_ocr.output_contract import failed_output

# Textract jobs are mostly waiting on AWS, so several prescriptions run in parallel.
TEXTRACT_CONCURRENCY = int(os.getenv("SIGCARE_TEXTRACT_CONCURRENCY", "4"))

LOG = logging.getLogger(__name__)


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


def _ocr_page_for_marker(page: fitz.Page, include_upright: bool = True) -> str:
    image = page_image(page, 250)
    if image.width < 1600:
        scale = 1600 / image.width
        image = image.resize((int(image.width * scale), int(image.height * scale)))

    orientations = [
        image.transpose(Image.Transpose.ROTATE_90),
        image.transpose(Image.Transpose.ROTATE_180),
        image.transpose(Image.Transpose.ROTATE_270),
    ]
    if include_upright:
        orientations.insert(0, image)

    for orientation in orientations:
        try:
            text = ocr_image(orientation)
        except RuntimeError as exc:
            # Timed out: the other orientations of the same page would too.
            LOG.warning("Page-counter OCR stopped on page %d: %s", page.number + 1, exc)
            return ""
        if _find_page_marker(text)[0] is not None:
            return text
    return ""


def _get_marker(page: fitz.Page, ocr_text: str | None = None) -> tuple[int | None, int | None]:
    # Prefer the footer, then the selectable text layer, then OCR text already
    # produced by front-sheet detection, then fresh OCR of rotated renders.
    text = _footer_text(page)
    marker = _find_page_marker(text)
    if marker[0] is not None:
        return marker

    marker = _find_page_marker(page.get_text("text", sort=True))
    if marker[0] is not None:
        return marker

    if ocr_text:
        marker = _find_page_marker(ocr_text)
        if marker[0] is not None:
            return marker

    try:
        # The upright render was already OCR'd when ocr_text is supplied.
        return _find_page_marker(
            _ocr_page_for_marker(page, include_upright=ocr_text is None)
        )
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


def split_pdf(
    input_pdf: Path,
    split_dir: Path,
    ocr_text_by_page: list[str] | None = None,
    original_pages: list[int] | None = None,
    stem: str | None = None,
) -> tuple[list[Path], list[list[int]], list[str]]:
    """Split into separate PDFs and write split_report.txt.

    original_pages maps each page of input_pdf to its 1-based page number in the
    source PDF (front sheets may have been removed). Returns the output files,
    the original page numbers in each output, and the report lines.
    """
    split_dir.mkdir(parents=True, exist_ok=True)
    split_report: list[str] = []
    outputs: list[Path] = []
    output_pages: list[list[int]] = []
    stem = stem or input_pdf.stem

    with fitz.open(input_pdf) as source:
        if source.needs_pass:
            raise ValueError("Password-protected PDFs are not supported.")
        if source.page_count == 0:
            raise ValueError("The PDF contains no pages.")

        original_pages = original_pages or list(range(1, source.page_count + 1))
        markers: list[tuple[int | None, int | None]] = []
        for page_index, page in enumerate(source):
            ocr_text = ocr_text_by_page[page_index] if ocr_text_by_page else None
            marker = _get_marker(page, ocr_text)
            markers.append(marker)
            if marker[0] is None:
                split_report.append(
                    f"Source page {original_pages[page_index]}: No marker detected"
                )
            else:
                split_report.append(
                    f"Source page {original_pages[page_index]}: Page {marker[0]} of {marker[1]}"
                )

        groups = _group_pages(markers)
        for number, indices in enumerate(groups, start=1):
            target = fitz.open()
            try:
                for index in indices:
                    target.insert_pdf(source, from_page=index, to_page=index)
                target_path = split_dir / f"{stem}_prescription_{number:03d}.pdf"
                target.save(target_path, garbage=4, deflate=True)
                outputs.append(target_path)
            finally:
                target.close()
            pages = [original_pages[i] for i in indices]
            output_pages.append(pages)
            split_report.append(
                f"Output {number}: source pages " + ", ".join(str(p) for p in pages)
            )

    (split_dir / "split_report.txt").write_text("\n".join(split_report), encoding="utf-8")
    return outputs, output_pages, split_report


def remove_pages(input_pdf: Path, output_pdf: Path, drop_pages: set[int]) -> list[int]:
    """Write a copy of input_pdf without drop_pages (1-based); return kept page numbers."""
    with fitz.open(input_pdf) as source:
        kept = [n for n in range(1, source.page_count + 1) if n not in drop_pages]
        if kept:
            target = fitz.open()
            try:
                for n in kept:
                    target.insert_pdf(source, from_page=n - 1, to_page=n - 1)
                target.save(output_pdf, garbage=4, deflate=True)
            finally:
                target.close()
    return kept


# --------------------------- Job processing ---------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    """Write JSON atomically so pollers never read a half-written file."""
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


class JobStatus:
    """status.json inside a job directory, updated as the job moves through stages."""

    def __init__(self, job_dir: Path, job_id: str, source_file_name: str):
        self.path = job_dir / "status.json"
        self.data: dict[str, Any] = {
            "job_id": job_id,
            "status": "QUEUED",
            "stage": None,
            "progress": None,
            "source_file_name": source_file_name,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "error": None,
        }
        self.save()

    def save(self) -> None:
        self.data["updated_at"] = now_iso()
        write_json(self.path, self.data)

    def update(self, **fields: Any) -> None:
        self.data.update(fields)
        self.save()


def extract_one(
    split_file: Path,
    number: int,
    job_id: str,
    original_pdf_location: str = "",
    prescription_location: str = "",
) -> tuple[dict[str, Any], str, str | None]:
    """Run Textract for one prescription; never raises so other results survive."""
    try:
        result = extract_prescription_pdf(
            pdf_path=split_file,
            prescription_number=number,
            job_id=job_id,
            original_pdf_location=original_pdf_location,
            prescription_location=prescription_location,
        )
        status = "REVIEW_REQUIRED" if result.get("flag_for_review") else "EXTRACTED"
        return result, status, None
    except Exception as exc:
        LOG.exception("Textract extraction failed for %s", split_file.name)
        error = f"{type(exc).__name__}: {exc}"
        source = prescription_source(split_file, number, job_id,
                                     original_pdf_location, prescription_location)
        return failed_output(source, [
            "Structured extraction failed; manual review is required.",
            error,
        ]), "ERROR", error


def process_document(
    input_pdf: Path,
    job_dir: Path,
    job_id: str,
    source_file_name: str,
    status: JobStatus,
    dpi: int = 220,
    create_cleanup: bool = True,
    split_stem: str = "uploaded",
    path_base: Path | None = None,
    original_pdf_location: str = "",
    artifact_location: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Run the full pipeline on input_pdf, writing every artifact under job_dir.

    The source PDF is never modified. Artifact paths in the result are relative
    to path_base (default: the parent of job_dir). Writes job_dir/result.json and
    returns the same dict; raises if a non-recoverable stage fails.

    original_pdf_location and artifact_location (relative path -> final location,
    e.g. an s3:// URI) fill the links in each prescription's JSON.
    """
    path_base = path_base or job_dir.parent
    artifact_location = artifact_location or (lambda relative_path: relative_path)
    split_dir = job_dir / "split_prescriptions"
    processed_dir = job_dir / "processed"
    frontsheet_dir = job_dir / "frontsheet_reports"
    started = time.monotonic()

    def relative(path: Path) -> str:
        return path.relative_to(path_base).as_posix()

    with fitz.open(input_pdf) as doc:
        if doc.needs_pass:
            raise ValueError("Password-protected PDFs are not supported.")
        source_page_count = doc.page_count
        if source_page_count < 1:
            raise ValueError("The PDF contains no pages.")

    # Stage 1: identify front sheets in the original PDF. The OCR text is reused
    # for page-counter detection so each page is only OCR'd once.
    status.update(status="RUNNING", stage="FRONTSHEET", progress=None)
    stage_start = time.monotonic()
    frontsheet_report_path = frontsheet_dir / "frontsheet_report.json"
    frontsheet_results = detect_frontsheets(
        input_pdf=input_pdf,
        output_json=frontsheet_report_path,
        dpi=250,
    )
    likely_frontsheet_pages = [
        item["source_page"]
        for item in frontsheet_results
        if item.get("likely_frontsheet") is True
    ]
    blank_pages = [
        item["source_page"]
        for item in frontsheet_results
        if item.get("likely_blank") is True and item.get("likely_frontsheet") is not True
    ]
    removed_pages = sorted(set(likely_frontsheet_pages) | set(blank_pages))
    manual_review_pages = [
        item["source_page"]
        for item in frontsheet_results
        if item.get("manual_review_required") is True
    ]
    LOG.info(
        "[%s] front-sheet detection: %d pages, front sheets %s, blank pages %s (%.1fs)",
        job_id, len(frontsheet_results), likely_frontsheet_pages, blank_pages,
        time.monotonic() - stage_start,
    )

    # Stage 2: remove front sheets and blank pages, then split the remaining pages.
    status.update(stage="SPLITTING")
    stage_start = time.monotonic()
    prescriptions_pdf = split_dir.parent / "frontsheets_removed.pdf"
    kept_pages = remove_pages(input_pdf, prescriptions_pdf, set(removed_pages))
    ocr_text = {item["source_page"]: item.get("ocr_text") or "" for item in frontsheet_results}

    if kept_pages:
        split_files, split_pages, _ = split_pdf(
            prescriptions_pdf,
            split_dir,
            ocr_text_by_page=[ocr_text.get(n, "") for n in kept_pages],
            original_pages=kept_pages,
            stem=split_stem,
        )
    else:
        split_dir.mkdir(parents=True, exist_ok=True)
        (split_dir / "split_report.txt").write_text(
            "All pages were detected as front sheets or blank; nothing to split.", encoding="utf-8"
        )
        split_files, split_pages = [], []
    LOG.info(
        "[%s] split into %d prescriptions (%.1fs)",
        job_id, len(split_files), time.monotonic() - stage_start,
    )

    # Stages 3 and 4 overlap: Textract only needs the split PDFs, so its jobs
    # (mostly waiting on AWS) run in background threads while quality and
    # cleanup use the CPU here.
    stage_start = time.monotonic()
    extractions: dict[int, tuple[dict[str, Any], str, str | None]] = {}
    quality_reports: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=max(1, TEXTRACT_CONCURRENCY),
        thread_name_prefix="sigcare-textract",
    ) as pool:
        futures = {
            pool.submit(extract_one, split_file, number, job_id, original_pdf_location,
                        artifact_location(relative(split_file))): number
            for number, split_file in enumerate(split_files, start=1)
        }

        # Stage 3: quality assessment and cleanup (CPU bound, sequential).
        for number, split_file in enumerate(split_files, start=1):
            status.update(stage="QUALITY", progress=f"{number}/{len(split_files)}")
            output_dir = processed_dir / split_file.stem
            output_dir.mkdir(parents=True, exist_ok=True)
            quality_reports.append(run_quality_pipeline(
                input_pdf=split_file,
                output_dir=output_dir,
                dpi=dpi,
                create_cleanup=create_cleanup,
            ))
        LOG.info("[%s] quality + cleanup done (%.1fs)", job_id, time.monotonic() - stage_start)

        # Stage 4: collect the Textract results.
        done = sum(future.done() for future in futures)
        if split_files:
            status.update(stage="TEXTRACT", progress=f"{done}/{len(split_files)}")
        for future in as_completed(futures):
            extractions[futures[future]] = future.result()
            status.update(progress=f"{len(extractions)}/{len(split_files)}")
    LOG.info("[%s] Textract extraction done (%.1fs since quality started)",
             job_id, time.monotonic() - stage_start)

    results: list[dict[str, Any]] = []
    for number, split_file in enumerate(split_files, start=1):
        output_dir = processed_dir / split_file.stem
        report = quality_reports[number - 1]
        prescription, extraction_status, extraction_error = extractions[number]

        # The prescription in the downstream contract; the full quality report
        # stays in quality_report.json rather than being repeated here.
        prescription_json_path = output_dir / "prescription.json"
        write_json(prescription_json_path, prescription)

        results.append({
            "prescription_number": number,
            "source_pages": split_pages[number - 1],
            "quality_status": report.get("overall_status", "UNKNOWN"),
            "quality_report_json": relative(output_dir / "quality_report.json"),
            "cleaned_pages_directory": (
                relative(output_dir / "cleaned_pages") if create_cleanup else None
            ),
            "extraction_status": extraction_status,
            "extraction_error": extraction_error,
            "prescription_json": relative(prescription_json_path),
            "prescription": prescription,
        })

    overall_status = (
        "COMPLETED_WITH_REVIEW"
        if any(
            r["quality_status"] != "PASS" or r["extraction_status"] != "EXTRACTED"
            for r in results
        )
        else "COMPLETED"
    )
    response = {
        "job_id": job_id,
        "status": overall_status,
        "source_file_name": source_file_name,
        "source_page_count": source_page_count,
        "prescriptions_created": len(split_files),
        "prescriptions_processed": len(results),
        "processing_seconds": round(time.monotonic() - started, 1),
        "split_report": relative(split_dir / "split_report.txt"),
        "frontsheet_report": relative(frontsheet_report_path),
        "frontsheet_detection": {
            "pages_scanned": len(frontsheet_results),
            "likely_frontsheet_pages": likely_frontsheet_pages,
            "blank_pages": blank_pages,
            "manual_review_pages": manual_review_pages,
            "removal_performed": bool(removed_pages),
            "removed_pages": removed_pages,
        },
        "results": results,
        "note": (
            "Technical screening only; PASS is not proof of authenticity. "
            "Review all flagged pages and compare cleaned images with the unchanged original."
        ),
    }
    write_json(job_dir / "result.json", response)
    status.update(status=overall_status, stage="DONE", progress=None)
    LOG.info("[%s] finished %s in %.1fs", job_id, overall_status, time.monotonic() - started)
    return response
