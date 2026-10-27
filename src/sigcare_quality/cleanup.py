
import cv2
import numpy as np
from PIL import Image


from .cleaner import clean_page, CleanerConfig

from .quality import estimate_skew


def _to_gray(image: Image.Image) -> np.ndarray:
    """Convert an image to an 8-bit grayscale array."""
    return np.array(image.convert("L"))


def _remove_uneven_background(gray: np.ndarray) -> np.ndarray:
    """
    Reduce gray shadows and uneven paper background.
    Keeps dark text and handwriting where possible.
    """
    gray = gray.astype(np.uint8)

    # Estimate slowly changing background using a large blur.
    h, w = gray.shape
    sigma = max(15.0, min(h, w) * 0.025)

    background = cv2.GaussianBlur(
        gray,
        (0, 0),
        sigmaX=sigma,
        sigmaY=sigma,
    )

    # Normalize local brightness without directly deleting dark details.
    normalized = (
        gray.astype(np.float32)
        * 245.0
        / np.maximum(background.astype(np.float32), 1.0)
    )

    return np.clip(normalized, 0, 255).astype(np.uint8)


def _denoise(gray: np.ndarray) -> np.ndarray:
    """Reduce scan noise conservatively."""
    return cv2.fastNlMeansDenoising(
        gray,
        None,
        h=6,
        templateWindowSize=7,
        searchWindowSize=21,
    )


def _deskew(gray: np.ndarray) -> np.ndarray:
    """Correct small page skew without changing the source image."""
    angle = estimate_skew(gray)

    if 0.3 <= abs(angle) <= 7.0:
        h, w = gray.shape
        matrix = cv2.getRotationMatrix2D(
            (w / 2, h / 2), angle, 1.0
        )

        gray = cv2.warpAffine(
            gray,
            matrix,
            (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )

    return gray


def clean_for_ocr(image: Image.Image) -> Image.Image:
    """Clean a prescription image using the Agent 06 pipeline."""
    gray = np.array(image.convert("L"))
    cleaned = clean_page(gray, config=CleanerConfig())
    return Image.fromarray(cleaned)


def reduce_prescription_noise(image: Image.Image) -> Image.Image:
    """Mildly reduce speckles while preserving fine details."""
    gray = _to_gray(image)
    cleaned = _remove_uneven_background(gray)
    cleaned = cv2.medianBlur(cleaned, 3)

    return Image.fromarray(cleaned)


def create_cleanup_variants(image: Image.Image) -> dict[str, Image.Image]:
    """Create alternatives for visual comparison."""
    gray = _to_gray(image)

    # Variant 1: Minimal processing.
    mild = cv2.medianBlur(gray, 3)

    # Variant 2: Background normalization plus gentle denoising.
    background_clean = _remove_uneven_background(gray)
    background_clean = _denoise(background_clean)

    # Variant 3: Background normalization plus mild speckle reduction.
    denoised = cv2.medianBlur(background_clean, 3)

    # Variant 4: High-contrast version; may lose faint writing.
    adaptive = cv2.adaptiveThreshold(
        background_clean,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        10,
    )

    return {
        "mild": Image.fromarray(mild),
        "background_clean": Image.fromarray(background_clean),
        "denoised": Image.fromarray(denoised),
        "adaptive": Image.fromarray(adaptive),
    }

