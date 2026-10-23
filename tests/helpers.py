"""Utilitaires de test partagés."""
import io

import numpy as np
from PIL import Image


def jpeg_bytes(w=320, h=240, color=(120, 160, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def noisy_png_rgba(w=120, h=80, alpha_value=255) -> bytes:
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 255, (h, w, 4), dtype=np.uint8)
    arr[:, :, 3] = alpha_value
    arr[:, : w // 2, 3] = 0  # moitié gauche transparente
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


def noisy_jpeg_bytes(w=700, h=500) -> bytes:
    """JPEG incompressible (bruit) : taille de l ordre de plusieurs centaines de Ko."""
    rng = np.random.default_rng(1)
    arr = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=95)
    return buf.getvalue()
