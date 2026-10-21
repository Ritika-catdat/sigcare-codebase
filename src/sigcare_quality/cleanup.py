import cv2
import numpy as np
from PIL import Image
from .quality import estimate_skew

def clean_for_ocr(image: Image.Image) -> Image.Image:
    """Create a separate OCR derivative; never overwrite source or remove marks."""
    rgb = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    denoised = cv2.fastNlMeansDenoising(gray, None, h=8, templateWindowSize=7, searchWindowSize=21)
    enhanced = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(denoised)
    angle = estimate_skew(enhanced)
    if 0.3 <= abs(angle) <= 7.0:
        h, w = enhanced.shape
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        enhanced = cv2.warpAffine(enhanced, matrix, (w, h),
                                  flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(enhanced)





def reduce_prescription_noise(image: Image.Image) -> Image.Image:
    """Conservative speckle reduction for a test OCR copy."""
    gray = np.array(image.convert("L"))

    # Mild reduction of isolated black-and-white noise.
    denoised = cv2.medianBlur(gray, 3)

    return Image.fromarray(denoised)



def create_cleanup_variants(image: Image.Image) -> dict:
    """Create alternative cleanup versions for local visual comparison."""
    gray = np.array(image.convert("L"))

    # Variant 1: Conservative denoising
    mild = cv2.medianBlur(gray, 3)

    # Variant 2: Non-local Means denoising
    denoised = cv2.fastNlMeansDenoising(
        gray, None, h=10,
        templateWindowSize=7,
        searchWindowSize=21
    )

    # Variant 3: Adaptive thresholding
    adaptive = cv2.adaptiveThreshold(
        denoised, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31, 10
    )

    return {
        "mild": Image.fromarray(mild),
        "denoised": Image.fromarray(denoised),
        "adaptive": Image.fromarray(adaptive),
    }

