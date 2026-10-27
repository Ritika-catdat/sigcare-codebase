"""Array-in / array-out Stage-2 cleaning.

Replaces the standalone tool's ``DarkForegroundOCRCleaner.process``, which was
path-in/path-out and forced a PNG round-trip through local disk between the
two stages — unacceptable both for latency and for identity-document PII
hygiene. Same processing steps, no filesystem, no debug dumps.
"""

from __future__ import annotations

import numpy as np

from .config import CleanerConfig
from .enhance import enhance_image
from .foreground import (
    build_dark_foreground_mask,
    build_foreground_contours,
    calculate_foreground_threshold,
)
from .output import render_output
from .reconstruct import reconstruct_foreground_mask


def clean_page(
    gray: np.ndarray,
    config: CleanerConfig | None = None,
    max_output_px: int = 9500,
) -> np.ndarray:
    """Clean one grayscale page (Stage-1 output) into the OCR rendering.

    ``max_output_px`` caps the longest output edge: the configured upscale is
    reduced (down to 1x) whenever it would exceed the cap, keeping every
    rendering under Textract's 10,000 px hard limit regardless of input size.
    """
    cfg = config or CleanerConfig()

    enhanced_data = enhance_image(gray, cfg)
    enhanced = enhanced_data["enhanced"]

    threshold = calculate_foreground_threshold(enhanced, cfg)
    foreground_mask = build_dark_foreground_mask(enhanced, threshold)
    _, contour_mask = build_foreground_contours(foreground_mask, cfg)
    reconstructed_mask, _ = reconstruct_foreground_mask(
        enhanced,
        foreground_mask,
        contour_mask,
        threshold,
        cfg,
    )

    longest_edge = max(gray.shape[:2])
    scale = max(1, min(int(cfg.upscale_factor), max_output_px // max(1, longest_edge)))

    return render_output(gray, reconstructed_mask, cfg, scale)
