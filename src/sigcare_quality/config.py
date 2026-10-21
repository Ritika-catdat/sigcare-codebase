from dataclasses import dataclass

@dataclass(frozen=True)
class QualityThresholds:
    # Starter thresholds only; calibrate on client-approved samples.
    min_width_px: int = 900
    min_height_px: int = 900
    min_mean_brightness: float = 35.0
    max_mean_brightness: float = 245.0
    min_contrast_std: float = 12.0
    min_laplacian_variance: float = 25.0
    blank_ink_fraction: float = 0.001
    max_abs_skew_degrees: float = 7.0

DEFAULT_THRESHOLDS = QualityThresholds()
