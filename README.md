# Sigcare Document Integrity & Image Cleanup

Local first-stage tool for Sigcare.

## Does
- Checks whether the PDF opens and pages render.
- Records SHA-256 and basic PDF metadata.
- Measures rendered dimensions, brightness, contrast, sharpness/blur, blank-page likelihood and possible skew.
- Flags technical anomalies for review; does not make a definitive genuine/fake determination.
- Creates cleaned OCR-oriented page images using grayscale, mild denoising, contrast enhancement and small-angle deskew.
- Preserves the source PDF unchanged and writes TXT + JSON reports.

## Important
This is a baseline screening tool, not a trained authenticity AI model. Genuine/suspicious classification requires client-approved definitions, labelled examples and validation. It never labels a document "genuine" or "fake".
It does **not remove watermarks or erase marks from prescriptions**. It creates a separate OCR copy and preserves all marks so the original remains auditable. Watermark removal could alter evidence.

## Windows setup
Use Python 3.11 or newer:
```bat
py --list
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -e .
```
If your installed Python is a different version >=3.11, replace `-3.11` accordingly.

Put your PDF in `data\input_pdf.pdf`, then run:
```bat
sigcare-quality data\input_pdf.pdf --output output
```
If the command is not found:
```bat
python -m sigcare_quality.cli data\input_pdf.pdf --output output
```

Outputs:
```text
output/
├── cleaned_pages/page_001.png, page_002.png, ...
├── quality_report.json
└── quality_report.txt
```

`PASS` means no configured technical rule fired; it does not mean genuine. `REVIEW` means inspect the original. Starter thresholds need calibration on representative Sigcare samples.
