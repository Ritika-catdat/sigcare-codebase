
import re
import argparse
from pathlib import Path

import fitz
from PIL import Image
import pytesseract


def get_footer_text(page):
    """Read text from the bottom 22% of a PDF page."""
    rect = page.rect
    footer = fitz.Rect(
        rect.x0,
        rect.y0 + rect.height * 0.78,
        rect.x1,
        rect.y1,
    )
    return page.get_text("text", clip=footer, sort=True)



def ocr_full_page(page):
    """OCR the entire page, trying multiple orientations."""
    rect = page.rect

    # Render the complete page, not just the footer.
    pix = page.get_pixmap(dpi=250, alpha=False)

    image = Image.frombytes(
        "RGB",
        [pix.width, pix.height],
        pix.samples,
    )

    # Enlarge small pages to improve recognition of tiny counters.
    if image.width < 1600:
        scale = 1600 / image.width
        image = image.resize((
            int(image.width * scale),
            int(image.height * scale),
        ))

    # Try the original orientation and rotated versions.
    orientations = [
        image,
        image.transpose(Image.Transpose.ROTATE_90),
        image.transpose(Image.Transpose.ROTATE_180),
        image.transpose(Image.Transpose.ROTATE_270),
    ]

    for oriented_image in orientations:
        text = pytesseract.image_to_string(
            oriented_image,
            config="--psm 11",
        )

        current, total = find_page_marker(text)

        if current is not None:
            return text

    # Return the original OCR result if no marker was found.
    return pytesseract.image_to_string(
        image,
        config="--psm 11",
    )


def find_page_marker(text):
    """Detect markers such as Page 1 of 2 or Page 1/2."""
    text = text.lower()
    text = text.replace("|", "1")

    pattern = r"(?:page\s*)?(\d{1,3})\s*(?:of|/)\s*(\d{1,3})"

    for match in reversed(list(re.finditer(pattern, text))):
        current = int(match.group(1))
        total = int(match.group(2))

        if 1 <= current <= total <= 200:
            return current, total

    return None, None



def group_pages(markers):
    """
    Group pages into prescriptions using page-of-total counters.

    Rules:
    1. '1 of N' starts a prescription containing N pages.
    2. A missing counter on a continuation page does not
       automatically split that prescription.
    3. A clearly detected '1 of N' starts a new prescription.
    4. Pages without a detectable start marker remain single
       pages unless they are expected continuation pages.
    """

    groups = []
    i = 0
    page_count = len(markers)

    while i < page_count:
        current, total = markers[i]

        # A reliable first-page counter tells us the expected
        # length of this prescription.
        if current == 1 and total is not None and total > 1:
            group = [i]
            expected_page = 2
            j = i + 1

            while j < page_count and expected_page <= total:
                next_current, next_total = markers[j]

                # A new, clearly detected prescription begins here.
                # Do not merge it into the current prescription.
                if next_current == 1:
                    break

                # If a counter exists, it must agree with the
                # expected page number and prescription total.
                if next_current is not None:
                    if (
                        next_current != expected_page
                        or next_total != total
                    ):
                        break

                # If the counter is missing, use the expected
                # page position, but record this for later review.
                group.append(j)
                expected_page += 1
                j += 1

            groups.append(group)
            i = j
            continue

        # A page with no reliable starting counter is kept separate.
        groups.append([i])
        i += 1

    return groups



def split_pdf(input_pdf, output_folder):
    input_pdf = Path(input_pdf)
    output_folder = Path(output_folder)

    if not input_pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {input_pdf}")

    output_folder.mkdir(parents=True, exist_ok=True)
    document = fitz.open(input_pdf)

    markers = []
    report = []

    # Detect footer counters on each source page.
    for index, page in enumerate(document):
    # First search ALL selectable text on the page.
     text = page.get_text("text", sort=True)
    current, total = find_page_marker(text)

    # If the marker isn't in the PDF text layer, OCR the entire page.
    if current is None:
        try:
            text = ocr_full_page(page)
            current, total = find_page_marker(text)

        except pytesseract.TesseractNotFoundError:
            raise RuntimeError(
                "Tesseract OCR is not installed. "
                "Install it or configure its executable path."
            )

    markers.append((current, total))

    if current is None:
        report.append(
            f"Source page {index + 1}: No marker detected"
        )
    else:
        report.append(
            f"Source page {index + 1}: "
            f"Page {current} of {total}"
        )


    groups = group_pages(markers)
    stem = input_pdf.stem

    for number, page_indices in enumerate(groups, start=1):
        output = fitz.open()

        for index in page_indices:
            output.insert_pdf(
                document,
                from_page=index,
                to_page=index,
            )

            page = output[-1]

            # Rotate landscape pages to portrait display.
            if page.rect.width > page.rect.height:
                page.set_rotation((page.rotation + 90) % 360)

        filename = f"{stem}_prescription_{number:03d}.pdf"
        output_path = output_folder / filename

        output.save(output_path, garbage=4, deflate=True)
        output.close()

        report.append(
            f"Output {number}: source pages "
            + ", ".join(str(i + 1) for i in page_indices)
        )

        print(f"Created: {output_path}")

    report_path = output_folder / "split_report.txt"
    report_path.write_text("\n".join(report), encoding="utf-8")

    document.close()

    print(f"\nTotal prescriptions created: {len(groups)}")
    print(f"Review the report: {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Split a combined prescription PDF."
    )
    parser.add_argument("input_pdf", help="Input PDF filename")
    parser.add_argument(
        "--output",
        default="output",
        help="Output folder (default: output)",
    )

    args = parser.parse_args()

    split_pdf(args.input_pdf, args.output)
