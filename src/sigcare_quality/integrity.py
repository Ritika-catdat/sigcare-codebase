from pathlib import Path
import hashlib
import os
import fitz
from PIL import Image

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def inspect_pdf(path: Path) -> dict:
    result = {"file_name": path.name, "file_size_bytes": path.stat().st_size,
              "sha256": sha256_file(path), "pdf_opened": False,
              "page_count": 0, "metadata": {}, "errors": []}
    try:
        with fitz.open(path) as doc:
            result["pdf_opened"] = True
            result["page_count"] = doc.page_count
            result["metadata"] = {k: v for k, v in (doc.metadata or {}).items() if v}
            if doc.needs_pass:
                result["errors"].append("PDF is password-protected.")
            if not doc.page_count:
                result["errors"].append("PDF contains no pages.")
    except Exception as exc:
        result["errors"].append(f"PDF could not be opened: {type(exc).__name__}: {exc}")
    return result

# Longest rendered edge in pixels. A4 at 300 dpi is 3508 px, so normal pages are
# unaffected; oversized pages (e.g. photos embedded at 72 dpi) would otherwise
# render to images so large that OCR and cleanup take minutes per page.
MAX_RENDER_PX = int(os.getenv("SIGCARE_MAX_RENDER_PX", "3500"))


def page_pixmap(page: fitz.Page, dpi: float) -> fitz.Pixmap:
    """Render a page at dpi, lowered if needed so no edge exceeds MAX_RENDER_PX."""
    longest_points = max(page.rect.width, page.rect.height)
    if longest_points > 0:
        dpi = min(dpi, MAX_RENDER_PX * 72 / longest_points)
    zoom = dpi / 72
    return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)


def page_image(page: fitz.Page, dpi: float) -> Image.Image:
    pix = page_pixmap(page, dpi)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def render_page(doc: fitz.Document, index: int, dpi: int = 220):
    page = doc.load_page(index)
    return page, page_image(page, dpi)
