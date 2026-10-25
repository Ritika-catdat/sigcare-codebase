import cv2
import numpy as np

from .config import CleanerConfig
from .utils import ensure_odd


def estimate_background(
    gray: np.ndarray,
    config: CleanerConfig,
) -> np.ndarray:
    """
    Estimate slow page illumination only.
    This image is never used as watermark evidence.
    """

    k = ensure_odd(config.background_kernel)

    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (k, k),
    )

    background = cv2.morphologyEx(
        gray,
        cv2.MORPH_CLOSE,
        kernel,
    )

    background = cv2.GaussianBlur(
        background,
        (0, 0),
        sigmaX=max(2.0, k / 7.0),
        sigmaY=max(2.0, k / 7.0),
    )

    return background


def normalize_illumination(
    gray: np.ndarray,
    background: np.ndarray,
) -> np.ndarray:
    g = gray.astype(np.float32)
    b = background.astype(np.float32)

    b[b < 1] = 1.0

    normalized = (g / b) * 255.0

    return np.clip(
        normalized,
        0,
        255,
    ).astype(np.uint8)


def enhance_image(
    gray: np.ndarray,
    config: CleanerConfig,
):
    """
    Mild enhancement only.

    Important:
    No residual-darkness map.
    No lighter-candidate extraction.
    No watermark segmentation.
    """

    k = max(
        1,
        int(config.denoise_kernel),
    )

    if k % 2 == 0:
        k += 1

    if k >= 3:
        denoised = cv2.medianBlur(
            gray,
            k,
        )
    else:
        denoised = gray.copy()

    background = estimate_background(
        denoised,
        config,
    )

    normalized = normalize_illumination(
        gray,  # preserve original edges
        background,
    )

    clahe = cv2.createCLAHE(
        clipLimit=float(config.clahe_clip_limit),
        tileGridSize=(
            int(config.clahe_grid_size),
            int(config.clahe_grid_size),
        ),
    )

    enhanced = clahe.apply(normalized)

    return {
        "background": background,
        "normalized": normalized,
        "enhanced": enhanced,
    }
