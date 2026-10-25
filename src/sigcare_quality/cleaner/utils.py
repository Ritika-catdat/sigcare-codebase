import cv2
import numpy as np


def ensure_odd(value: int) -> int:
    value = max(3, int(value))
    return value if value % 2 else value + 1


def unsharp_mask(
    gray: np.ndarray,
    amount: float = 1.05,
    sigma: float = 1.0,
) -> np.ndarray:
    blurred = cv2.GaussianBlur(
        gray,
        (0, 0),
        sigmaX=max(0.1, float(sigma)),
        sigmaY=max(0.1, float(sigma)),
    )

    sharp = cv2.addWeighted(
        gray,
        1.0 + float(amount),
        blurred,
        -float(amount),
        0,
    )

    return np.clip(
        sharp,
        0,
        255,
    ).astype(np.uint8)
