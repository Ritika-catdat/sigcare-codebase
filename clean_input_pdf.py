
from pathlib import Path

import fitz
from PIL import Image

from sigcare_quality.cleanup import clean_for_ocr

input_pdf = Path("input_pdf.pdf")
output_dir = Path("output") / "cleaned_pages"
output_dir.mkdir(parents=True, exist_ok=True)

if not input_pdf.exists():
    raise FileNotFoundError(f"Input PDF not found: {input_pdf.resolve()}")

with fitz.open(input_pdf) as pdf:
    total = len(pdf)

    for index, page in enumerate(pdf):
        pix = page.get_pixmap(
            matrix=fitz.Matrix(2, 2),
            colorspace=fitz.csRGB,
            alpha=False,
        )

        image = Image.frombytes(
            "RGB", (pix.width, pix.height), pix.samples
        )
        cleaned = clean_for_ocr(image)
        cleaned.save(output_dir / f"page_{index + 1:03d}.png")

        print(f"Processed page {index + 1}/{total}")

print(f"Finished. Output folder: {output_dir.resolve()}")
