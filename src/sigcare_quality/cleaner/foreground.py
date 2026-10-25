import cv2
import numpy as np

from .config import CleanerConfig


def calculate_foreground_threshold(
    enhanced: np.ndarray,
    config: CleanerConfig,
) -> int:
    """
    Calculate threshold from only non-paper pixels.

    Lower grayscale = darker.
    """

    values = enhanced.reshape(-1)

    content = values[values < 248]

    if content.size < 100:
        content = values

    threshold = float(
        np.percentile(
            content,
            config.foreground_percentile,
        )
    )

    return int(
        np.clip(
            threshold,
            config.min_foreground_threshold,
            config.max_foreground_threshold,
        )
    )


def build_dark_foreground_mask(
    enhanced: np.ndarray,
    threshold: int,
) -> np.ndarray:
    """
    Darkest pixels are the foreground source of truth.
    """

    mask = (enhanced <= threshold).astype(np.uint8) * 255

    # Remove only true single-pixel noise. One vectorised lookup instead of a
    # per-component full-image compare: with tens of thousands of ink
    # components the loop was O(components x pixels) and dominated runtime.
    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask,
        connectivity=8,
    )

    keep = stats[:, cv2.CC_STAT_AREA] >= 2
    keep[0] = False  # label 0 is the background

    return keep[labels].astype(np.uint8) * 255


def build_foreground_contours(
    foreground_mask: np.ndarray,
    config: CleanerConfig,
):
    protected = foreground_mask.copy()

    if config.contour_dilate_iterations > 0:
        protected = cv2.dilate(
            protected,
            np.ones((3, 3), np.uint8),
            iterations=int(config.contour_dilate_iterations),
        )

    contours, _ = cv2.findContours(
        protected,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )

    contour_mask = np.zeros_like(foreground_mask)

    cv2.drawContours(
        contour_mask,
        contours,
        -1,
        255,
        thickness=cv2.FILLED,
    )

    return contours, contour_mask
