from pathlib import Path
import hashlib
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

def render_page(doc: fitz.Document, index: int, dpi: int = 220):
    page = doc.load_page(index)
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return page, Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
