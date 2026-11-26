"""Black-and-white copies of tinted prescriptions for Textract.

Some prescriptions are printed or scanned on coloured paper (green, yellow). On those
pages Textract missed whole tables and misread table layouts, while the same forms on
white paper read correctly. Before extraction, every tinted page is converted to a
grayscale page with the tint removed, so all prescriptions reach Textract the same way.
Pages that are already white are copied unchanged. The original PDF is never modified.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

from .cleanup import _remove_uneven_background
from .integrity import page_image

LOG = logging.getLogger(__name__)

# Median colour saturation (0-255) of the paper, above which a page counts as tinted.
# White scans measure 0; the pale green Eastwood Park pages 18, the green Kirkham pages 49.
# Coloured ink (a pen note, a logo) covers too little of the page to move the median.
TINT_MIN_SATURATION = int(os.getenv("SIGCARE_TINT_MIN_SATURATION", "10"))
MONOCHROME_DPI = int(os.getenv("SIGCARE_MONOCHROME_DPI", "250"))
JPEG_QUALITY = 85


def paper_saturation(page: fitz.Page) -> int:
    """Median colour saturation of the light (paper) part of the page."""
    hsv = np.asarray(page_image(page, 40).convert("HSV"))
    paper = hsv[..., 2] > 120
    return int(np.median(hsv[..., 1][paper])) if paper.any() else 0


def is_tinted(page: fitz.Page) -> bool:
    return paper_saturation(page) >= TINT_MIN_SATURATION


def black_and_white(page: fitz.Page, dpi: int = MONOCHROME_DPI) -> bytes:
    """The page as a grayscale JPEG with the paper tint flattened to white."""
    gray = np.asarray(page_image(page, dpi).convert("L"))
    flat = _remove_uneven_background(gray)
    buffer = io.BytesIO()
    Image.fromarray(flat).save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def monochrome_copy(pdf_path: Path, output_dir: Path) -> Path | None:
    """Write a copy of pdf_path with its tinted pages in black and white.

    Returns the copy's path, or None when no page is tinted (the original is used).
    """
    with fitz.open(pdf_path) as source:
        tinted = [is_tinted(page) for page in source]
        if not any(tinted):
            return None
        output_dir.mkdir(parents=True, exist_ok=True)
        target_path = output_dir / f"{pdf_path.stem}_bw.pdf"
        target = fitz.open()
        try:
            for index, (page, convert) in enumerate(zip(source, tinted)):
                if not convert:
                    target.insert_pdf(source, from_page=index, to_page=index)
                    continue
                # page.rect is the page as displayed (its /Rotate applied), like the render.
                new_page = target.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(new_page.rect, stream=black_and_white(page))
            target.save(target_path, garbage=4, deflate=True)
        finally:
            target.close()
    LOG.info("Converted %d tinted page(s) of %s to black and white", sum(tinted), pdf_path.name,
             extra={"tinted_pages": sum(tinted)})
    return target_path
