"""Tinted prescriptions are sent to Textract in black and white; white pages are not touched."""
import fitz
import numpy as np

from sigcare_quality.integrity import page_image
from sigcare_quality.monochrome import is_tinted, monochrome_copy


def _pdf(path, backgrounds):
    doc = fitz.open()
    for colour in backgrounds:
        page = doc.new_page(width=300, height=400)
        if colour:
            page.draw_rect(page.rect, color=colour, fill=colour)
        page.insert_text((40, 60), "Drug name  Dose  Start date", fontsize=11)
        # A coloured pen note on a white page is ink, not a tinted page.
        page.draw_circle((250, 350), 12, color=(0.1, 0.2, 0.9), width=2)
    doc.save(path)


def test_only_tinted_pages_are_converted(tmp_path):
    pdf = tmp_path / "rx.pdf"
    _pdf(pdf, [(0.85, 0.95, 0.75), None])
    with fitz.open(pdf) as doc:
        assert [is_tinted(page) for page in doc] == [True, False]

    copy = monochrome_copy(pdf, tmp_path / "bw")
    assert copy is not None and copy.parent.name == "bw"
    with fitz.open(copy) as doc:
        assert doc.page_count == 2
        image = np.asarray(page_image(doc[0], 40).convert("HSV"))
        assert int(np.median(image[..., 1])) == 0                  # no colour left
        assert int(np.median(image[..., 2])) > 230                 # white paper
        assert "Drug name" in doc[1].get_text()                    # white page copied as is


def test_white_prescription_is_sent_unchanged(tmp_path):
    pdf = tmp_path / "rx.pdf"
    _pdf(pdf, [None, None])
    assert monochrome_copy(pdf, tmp_path / "bw") is None
    assert not (tmp_path / "bw").exists()
