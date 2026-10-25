
import re
from pathlib import Path

import fitz
import pytesseract
from PIL import Image


# These are initial indicators, not definitive proof of a front sheet.
FRONTSHEET_PATTERNS = {
    "sigma_reference": re.compile(r"\bsigma\b", re.IGNORECASE),
    "patient_details": re.compile(
        r"\b(patient details|patient name|prisoner number|offender number)\b",
        re.IGNORECASE,
    ),
    "prescription_summary": re.compile(
        r"\b(prescription summary|medication summary|front sheet)\b",
        re.IGNORECASE,
    ),
}


def extract_page_text(page, dpi=250):
    """Run local Tesseract OCR on a rendered PDF page."""
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    return pytesseract.image_to_string(
        image,
        config="--psm 11",
    ).strip()



def classify_frontsheet(text):
    """Identify likely Sigma front sheets using distinctive OCR text."""
    normalized = re.sub(r"\s+", " ", text).strip().lower()

    matches = []

    # Strong indicator: the explicit Sigma front-sheet heading.
    if re.search(r"\bsigma[\s._-]+front[\s._-]+sheet\b", normalized):
        matches.append("sigma_front_sheet_heading")

    # Supporting indicators commonly present on Sigma sheets.
    if re.search(r"\bitems?\s+ordered\b", normalized):
        matches.append("items_ordered")

    if re.search(r"\bmissing\s*\(?\s*m\s*\)?", normalized):
        matches.append("missing_m")

    if re.search(r"on[\s-]*to[\s-]*follow", normalized):
        matches.append("on_to_follow")

    if re.search(r"\bemail\s*\(?\s*e\s*\)?", normalized):
        matches.append("email")

    if re.search(
        r"attached to each order sent|cd envelopes",
        normalized,
    ):
        matches.append("attachment_instruction")

    # Require the distinctive heading plus supporting evidence,
    # or several supporting indicators if OCR misses the heading.
    heading_found = "sigma_front_sheet_heading" in matches
    supporting_count = len(matches) - int(heading_found)

    likely_frontsheet = (
        heading_found and supporting_count >= 1
    ) or (
        not heading_found and supporting_count >= 4
    )

    return {
        "likely_frontsheet": likely_frontsheet,
        "matched_indicators": matches,
        "manual_review_required": not likely_frontsheet,
    }



def detect_frontsheets(input_pdf, output_json, dpi=250):
    """Report possible front sheets without changing the source PDF."""
    input_path = Path(input_pdf)
    output_path = Path(output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    results = []

    with fitz.open(input_path) as document:
        for index, page in enumerate(document):
            try:
                text = extract_page_text(page, dpi=dpi)
                classification = classify_frontsheet(text)

                results.append({
                    "source_page": index + 1,
                    "ocr_text": text,
                    "ocr_engine": "Tesseract",
                    **classification,
                    "error": None,
                })

            except Exception as exc:
                results.append({
                    "source_page": index + 1,
                    "ocr_text": "",
                    "ocr_engine": "Tesseract",
                    "likely_frontsheet": False,
                    "matched_indicators": [],
                    "manual_review_required": True,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    import json

    output_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return results
