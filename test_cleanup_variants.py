
import sys
from pathlib import Path

import fitz
from PIL import Image

from sigcare_quality.cleanup import create_cleanup_variants


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python test_cleanup_variants.py "
            "<pdf_path> [page_number]"
        )
        return

    pdf_path = Path(sys.argv[1])
    page_number = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}")
        return

    output_dir = Path("cleanup_comparison")
    output_dir.mkdir(parents=True, exist_ok=True)

    with fitz.open(pdf_path) as doc:
        if not 1 <= page_number <= len(doc):
            print(f"Choose a page between 1 and {len(doc)}.")
            return

        page = doc[page_number - 1]

        # Render from the original PDF at 300 DPI.
        pix = page.get_pixmap(
            matrix=fitz.Matrix(300 / 72, 300 / 72),
            alpha=False,
        )

        image = Image.frombytes(
            "RGB", (pix.width, pix.height), pix.samples
        )

    image.save(output_dir / "original_render.png")

    variants = create_cleanup_variants(image)

    for name, result in variants.items():
        result.save(output_dir / f"{name}.png")

    print(f"Comparison images saved to: {output_dir.resolve()}")
    print("Compare original_render.png, mild.png, denoised.png,")
    print("and adaptive.png.")


if __name__ == "__main__":
    main()
