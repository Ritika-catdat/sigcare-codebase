import cv2
import numpy as np

from .config import CleanerConfig
from .utils import unsharp_mask


def render_mask_only_output(
    reconstructed_mask: np.ndarray,
    scale: int,
) -> np.ndarray:
    """
    Safest OCR output:
      foreground mask -> black
      everything else -> white

    Watermark/background cannot re-enter this image.
    """

    if scale > 1:
        high_mask = cv2.resize(
            reconstructed_mask,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_NEAREST,
        )
    else:
        high_mask = reconstructed_mask.copy()

    output = np.full(
        high_mask.shape,
        255,
        dtype=np.uint8,
    )

    output[high_mask > 0] = 0

    return output


def render_gray_foreground_output(
    original_gray: np.ndarray,
    reconstructed_mask: np.ndarray,
    config: CleanerConfig,
    scale: int,
):
    """
    Alternative OCR image:
    use original grayscale values ONLY where foreground mask exists.
    Everything else is white.

    This preserves anti-aliased foreground detail without bringing
    watermark/background back into the image.

    ``scale`` is passed explicitly (not read off the config) so the caller can
    clamp the output below Textract's dimension limits.
    """

    masked = np.full(
        original_gray.shape,
        255,
        dtype=np.uint8,
    )

    masked[reconstructed_mask > 0] = original_gray[reconstructed_mask > 0]

    scale = max(1, int(scale))

    if scale > 1:
        high = cv2.resize(
            masked,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_LANCZOS4,
        )
    else:
        high = masked.copy()

    high = unsharp_mask(
        high,
        amount=float(config.sharpen_amount),
        sigma=max(
            0.7,
            float(config.sharpen_sigma) * scale * 0.55,
        ),
    )

    # Force background to remain white after interpolation.
    if scale > 1:
        high_mask = cv2.resize(
            reconstructed_mask,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_NEAREST,
        )
    else:
        high_mask = reconstructed_mask

    high[high_mask == 0] = 255

    return high


def render_output(
    original_gray: np.ndarray,
    reconstructed_mask: np.ndarray,
    config: CleanerConfig,
    scale: int,
) -> np.ndarray:
    """The one OCR rendering the pipeline feeds to Textract.

    The standalone tool rendered *both* variants on every page and threw one
    away — at 2x+ upscale that doubled memory and time for nothing, so only
    the configured primary is built here.
    """
    if config.mask_only_output:
        return render_mask_only_output(reconstructed_mask, scale)
    return render_gray_foreground_output(original_gray, reconstructed_mask, config, scale)
