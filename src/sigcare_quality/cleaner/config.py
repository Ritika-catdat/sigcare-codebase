from dataclasses import dataclass


@dataclass(frozen=True)
class CleanerConfig:
    """Stage-2 (dark-foreground) tuning. Values are empirically tuned against
    real RTW scans — change them only alongside the cleaning regression tests.

    Vendoring changes vs the standalone tool: frozen (shared instances must be
    immutable), ``upscale_factor`` lowered from 5 to 2 (5× put a 3000 px Stage-1
    page at ~15,000 px, over Textract's 10,000 px hard limit — the effective
    scale is additionally clamped at render time, see ``clean.clean_page``),
    and the debug-dump switch removed (the service never writes identity
    documents to local disk).
    """

    # Step 2: light enhancement only
    denoise_kernel: int = 8
    background_kernel: int = 81
    clahe_clip_limit: float = 1.0
    clahe_grid_size: int = 8

    # Step 3: darkest pixels = real foreground
    foreground_percentile: float = 60.0
    min_foreground_threshold: int = 0
    max_foreground_threshold: int = 100

    # Step 4: contour protection
    contour_dilate_iterations: float = 2

    # Step 5/6: reconstruction from original scan
    reconstruction_radius: int = 1
    reconstruction_iterations: int = 4
    reconstruction_dark_margin: int = 4
    reconstruction_neighbor_required: int = 12

    # Optional stroke repair
    bridge_kernel_size: int = 2
    bridge_iterations: int = 1

    # Step 7: final OCR rendering
    upscale_factor: int = 4
    sharpen_amount: float = 1.05
    sharpen_sigma: float = 1.2

    # If True, final OCR image is rendered from the reconstructed mask only.
    # This completely excludes lighter watermark/background.
    mask_only_output: bool = True
