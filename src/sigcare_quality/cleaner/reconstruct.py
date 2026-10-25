import cv2
import numpy as np

from .config import CleanerConfig


def _neighbor_count(
    mask_bool: np.ndarray,
) -> np.ndarray:
    kernel = np.ones(
        (3, 3),
        dtype=np.uint8,
    )

    kernel[1, 1] = 0

    return cv2.filter2D(
        mask_bool.astype(np.uint8),
        cv2.CV_16U,
        kernel,
        borderType=cv2.BORDER_CONSTANT,
    )


def reconstruct_foreground_mask(
    enhanced: np.ndarray,
    foreground_mask: np.ndarray,
    contour_mask: np.ndarray,
    foreground_threshold: int,
    config: CleanerConfig,
):
    """
    Reconstruct only likely missing DARK foreground pixels.

    No lighter watermark/background pixels are ever added back merely
    because they exist in the image.

    A pixel is restored only when:
      - it is near an existing foreground contour
      - it is close in darkness to the trusted foreground threshold
      - it continues an existing dark stroke
    """

    result = foreground_mask > 0

    radius = max(
        1,
        int(config.reconstruction_radius),
    )

    search_region = (
        cv2.dilate(
            contour_mask,
            np.ones(
                (
                    radius * 2 + 1,
                    radius * 2 + 1,
                ),
                np.uint8,
            ),
            iterations=1,
        )
        > 0
    )

    restore_threshold = min(
        255,
        int(foreground_threshold + config.reconstruction_dark_margin),
    )

    source_dark = enhanced <= restore_threshold

    for _ in range(int(config.reconstruction_iterations)):
        neighbors = _neighbor_count(result)

        candidate = (
            search_region
            & source_dark
            & (~result)
            & (neighbors >= int(config.reconstruction_neighbor_required))
        )

        if not np.any(candidate):
            break

        result[candidate] = True

    reconstructed = result.astype(np.uint8) * 255

    # Very small close operation can reconnect broken character strokes.
    bridge_k = max(
        1,
        int(config.bridge_kernel_size),
    )

    if bridge_k > 1 and config.bridge_iterations > 0:
        reconstructed = cv2.morphologyEx(
            reconstructed,
            cv2.MORPH_CLOSE,
            np.ones(
                (bridge_k, bridge_k),
                np.uint8,
            ),
            iterations=int(config.bridge_iterations),
        )

    return reconstructed, restore_threshold
