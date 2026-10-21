def test_imports():
    from sigcare_quality.integrity import inspect_pdf
    from sigcare_quality.quality import assess_image
    from sigcare_quality.cleanup import clean_for_ocr
    assert callable(inspect_pdf) and callable(assess_image) and callable(clean_for_ocr)
