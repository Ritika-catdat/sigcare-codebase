import json
from pathlib import Path
import fitz
from .integrity import inspect_pdf, render_page
from .quality import assess_image
from .cleanup import clean_for_ocr


def run_quality_pipeline(input_pdf, output_dir, dpi=220, create_cleanup=True):
    source = Path(input_pdf)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    clean_dir = out / "cleaned_pages"
    if create_cleanup:
        clean_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "tool": "Sigcare Document Integrity & Image Cleanup",
        "input": inspect_pdf(source),
        "pages": [],
        "overall_status": "PASS",
        "important_note": (
            "PASS is not proof of authenticity. "
            "Review anomalies against the unchanged original."
        ),
    }

    if not report["input"]["pdf_opened"] or report["input"]["errors"]:
        report["overall_status"] = "ERROR"
        write_report(report, out)
        return report

    with fitz.open(source) as doc:
        for idx in range(doc.page_count):
            rec = {
                "source_page": idx + 1,
                "status": "ERROR",
                "cleanup_image": None,
                "quality": None,
                "errors": [],
            }

            try:
                page, image = render_page(doc, idx, dpi)

                rec["page_width_points"] = round(page.rect.width, 2)
                rec["page_height_points"] = round(page.rect.height, 2)
                rec["page_orientation"] = (
                    "LANDSCAPE"
                    if page.rect.width > page.rect.height
                    else "PORTRAIT"
                )

                # Run cleanup independently from quality assessment.
                if create_cleanup:
                    name = f"page_{idx + 1:03d}.png"
                    image_path = clean_dir / name

                    try:
                        cleaned = clean_for_ocr(image)
                        cleaned.save(image_path)
                        rec["cleanup_image"] = f"cleaned_pages/{name}"
                    except Exception as exc:
                        rec["errors"].append(
                            f"Cleanup failed: {type(exc).__name__}: {exc}"
                        )

                # Assess quality even if cleanup fails.
                try:
                    quality = assess_image(image)
                    rec["quality"] = quality
                except Exception as exc:
                    rec["errors"].append(
                        f"Quality assessment failed: "
                        f"{type(exc).__name__}: {exc}"
                    )

                if rec["errors"]:
                    rec["status"] = "ERROR"
                    report["overall_status"] = "REVIEW"
                elif rec["quality"]["status"] != "PASS":
                    rec["status"] = "REVIEW"
                    report["overall_status"] = "REVIEW"
                else:
                    rec["status"] = "PASS"

            except Exception as exc:
                rec["errors"].append(
                    f"Page rendering failed: {type(exc).__name__}: {exc}"
                )
                rec["status"] = "ERROR"
                report["overall_status"] = "REVIEW"

            report["pages"].append(rec)

    write_report(report, out)
    return report
 

def write_report(report, out):
    (out / "quality_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["SIGCARE DOCUMENT INTEGRITY & IMAGE CLEANUP REPORT", "="*52,
             f"Input: {report['input'].get('file_name')}",
             f"SHA-256: {report['input'].get('sha256')}",
             f"PDF opened: {report['input'].get('pdf_opened')}",
             f"Page count: {report['input'].get('page_count')}",
             f"Overall status: {report.get('overall_status')}",
             "", "Technical screening only; not a genuine/fake determination.", ""]
    for p in report.get("pages", []):
        lines.append(f"Source page {p.get('source_page')}: {p.get('status')}")
        if "quality" in p:
            q = p["quality"]
            lines += [f"  Orientation: {p['page_orientation']}",
                      f"  Dimensions: {q['width_px']} x {q['height_px']} px",
                      f"  Brightness: {q['mean_brightness']}",
                      f"  Contrast: {q['contrast_std']}",
                      f"  Sharpness: {q['sharpness_laplacian_variance']}",
                      f"  Estimated skew: {q['estimated_skew_degrees']} degrees",
                      f"  Findings: {', '.join(q['findings']) if q['findings'] else 'None'}",
                      f"  OCR copy: {p.get('cleanup_image') or 'Not generated'}"]
        elif p.get("error"):
            lines.append(f"  Error: {p['error']}")
        lines.append("")
    (out / "quality_report.txt").write_text("\n".join(lines), encoding="utf-8")
