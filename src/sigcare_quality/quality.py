import cv2
import numpy as np
from PIL import Image
from .config import DEFAULT_THRESHOLDS


def estimate_skew(gray):
    """Estimate page skew safely across OpenCV line-array shapes."""
    edges = cv2.Canny(
        cv2.GaussianBlur(gray, (3, 3), 0),
        50,
        150
    )

    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 1800,
        threshold=100,
        minLineLength=max(100, gray.shape[1] // 8),
        maxLineGap=15
    )

    if lines is None:
        return 0.0

    # Normalize OpenCV output to rows of four coordinates.
    try:
        line_coordinates = np.asarray(lines).reshape(-1, 4)
    except (ValueError, TypeError):
        return 0.0

    angles = []

    for line in line_coordinates:
        x1, y1, x2, y2 = map(int, line)

        angle = float(
            np.degrees(np.arctan2(y2 - y1, x2 - x1))
        )

        if abs(angle) <= 15:
            angles.append(angle)

    return float(np.median(angles)) if angles else 0.0
 

def assess_image(image: Image.Image) -> dict:
    gray = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    brightness = float(np.mean(gray))
    contrast = float(np.std(gray))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    ink_fraction = float(np.mean(gray < 200))
    skew = estimate_skew(gray)
    t = DEFAULT_THRESHOLDS
    findings = []
    if w < t.min_width_px or h < t.min_height_px: findings.append("LOW_RENDERED_RESOLUTION")
    if brightness < t.min_mean_brightness: findings.append("PAGE_TOO_DARK")
    if brightness > t.max_mean_brightness: findings.append("PAGE_TOO_BRIGHT")
    if contrast < t.min_contrast_std: findings.append("LOW_CONTRAST")
    if sharpness < t.min_laplacian_variance: findings.append("POSSIBLE_BLUR")
    if ink_fraction < t.blank_ink_fraction: findings.append("POSSIBLY_BLANK_PAGE")
    if abs(skew) > t.max_abs_skew_degrees: findings.append("POSSIBLE_SKEW")
    return {"width_px": w, "height_px": h,
            "mean_brightness": round(brightness, 2),
            "contrast_std": round(contrast, 2),
            "sharpness_laplacian_variance": round(sharpness, 2),
            "dark_pixel_fraction": round(ink_fraction, 5),
            "estimated_skew_degrees": round(skew, 2),
            "findings": findings, "status": "REVIEW" if findings else "PASS",
            "note": "Technical image indicators only; not an authenticity verdict."}
