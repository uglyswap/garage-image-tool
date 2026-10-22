"""Pipeline de retouche : détourage rembg, fond de remplacement, flou des plaques.

Tout est 100% local (CPU) : rembg (U2-Net) pour le détourage, OpenCV pour
le flou, Pillow pour l'encodage. Aucune API externe.
"""
from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

from . import config
from .plate_detector import PlateBox, detector

logger = logging.getLogger(__name__)

_session_lock = threading.Lock()
_sessions = {}
_birefnet_gate = threading.Semaphore(1)


def _get_session(model):
    """Session rembg par modèle (lazy), repli u2net."""
    with _session_lock:
        if model not in _sessions:
            from rembg import new_session

            try:
                _sessions[model] = new_session(model)
                logger.info("Session rembg %s initialisée", model)
            except Exception:  # noqa: BLE001
                logger.exception("Modèle %s indisponible, repli u2net", model)
                _sessions[model] = new_session("u2net")
    return _sessions[model]


def _get_rembg_session():
    """Compatibilité préchauffage : session modèle par défaut."""
    return _get_session(config.REMBG_MODEL)


def _drop_small_islands(rgba: Image.Image, keep_ratio: float = 0.15) -> Image.Image:
    """Supprime les petits fragments flottants du masque alpha.

    Ne garde que la composante connexe principale (le véhicule) et les îles
    secondaires représentant au moins keep_ratio de son aire (remorque...)."""
    arr = np.array(rgba.getchannel("A"))
    mask = (arr > 25).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 2:
        return rgba
    areas = stats[1:, cv2.CC_STAT_AREA]
    main_label = int(areas.argmax()) + 1
    main_area = int(areas.max())
    keep = {main_label}
    for i in range(1, n):
        if i != main_label and int(areas[i - 1]) >= keep_ratio * main_area:
            keep.add(i)
    cleaned = np.where(np.isin(labels, list(keep)), arr, 0).astype(np.uint8)
    rgba.putalpha(Image.fromarray(cleaned))
    return rgba


@dataclass
class ProcessOptions:
    bg_mode: str = "original"        # original | replace | transparent
    background: bytes | None = None
    blur_enabled: bool = True
    blur_strength: int = 3           # 1..5
    out_format: str = "jpeg"         # jpeg | png (forcé PNG si transparent)
    jpeg_quality: int = 92
    rembg_model: str = "isnet-general-use"  # isnet-general-use | birefnet-general-lite


@dataclass
class ProcessResult:
    data: bytes
    fmt: str                         # "jpeg" | "png"
    plates_blurred: int
    width: int
    height: int


def process_image(data: bytes, opts: ProcessOptions) -> ProcessResult:
    """Traite une image (bytes JPEG/PNG/WebP) et renvoie l'image finale."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if image.mode != "RGB":
        image = image.convert("RGB")

    bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    plates: list[PlateBox] = detector.detect(bgr) if opts.blur_enabled else []

    alpha_arr = None
    cutout = None
    if opts.bg_mode in ("transparent", "replace"):
        cutout = _remove_background(image, opts.rembg_model)
        alpha_arr = np.asarray(cutout.getchannel("A"))

    if opts.bg_mode == "transparent":
        out_img = cutout
    elif opts.bg_mode == "replace" and opts.background:
        bg = _load_background(opts.background, image.size)
        out_img = Image.alpha_composite(bg.convert("RGBA"), cutout)
    else:
        out_img = image.copy()

    if plates:
        rgb = np.asarray(out_img.convert("RGB"))
        bgr2 = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        blurred = 0
        for box in plates:
            # Si l objet porteur de la plaque a ete retire du cutout
            # (nettoyage ilots), inutile de flouter : tache flottante.
            if alpha_arr is not None:
                roi_alpha = alpha_arr[box.y1 : box.y2, box.x1 : box.x2]
                if roi_alpha.size and float(roi_alpha.mean()) < 30:
                    continue
            _blur_plate(bgr2, box, opts.blur_strength)
            blurred += 1
        out_img = Image.fromarray(cv2.cvtColor(bgr2, cv2.COLOR_BGR2RGB))
        if alpha_arr is not None:
            out_img = out_img.convert("RGBA")
            out_img.putalpha(Image.fromarray(alpha_arr))

    fmt = "png" if (opts.out_format == "png" or alpha_arr is not None) else "jpeg"
    buf = io.BytesIO()
    if fmt == "jpeg":
        out_img.convert("RGB").save(buf, "JPEG", quality=opts.jpeg_quality, optimize=True, progressive=True)
    else:
        out_img.save(buf, "PNG", optimize=True)
    return ProcessResult(buf.getvalue(), fmt, blurred if plates else 0, image.width, image.height)


# ---------------------------------------------------------------------
def _remove_background(image: Image.Image, model: str = None) -> Image.Image:
    from rembg import remove

    model = model or config.REMBG_MODEL
    session = _get_session(model)
    if model.startswith("birefnet"):
        # BiRefNet est gourmand : on sérialise les découpes Max pour éviter tout OOM
        with _birefnet_gate:
            return _drop_small_islands(remove(image, session=session, post_process_mask=True))
    return _drop_small_islands(remove(image, session=session, post_process_mask=True))


def _load_background(bg_bytes: bytes, size: tuple[int, int]) -> Image.Image:
    """Charge le fond et le recadre en 'cover' aux dimensions de l'image."""
    bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
    tw, th = size
    sw, sh = bg.size
    scale = max(tw / sw, th / sh)
    nw, nh = max(tw, int(round(sw * scale))), max(th, int(round(sh * scale)))
    bg = bg.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return bg.crop((left, top, left + tw, top + th))


def _blur_plate(arr_bgr: np.ndarray, box: PlateBox, strength: int) -> None:
    """Floute *uniquement* l'intérieur de la plaque (masque adouci).

    Combinaison pixelisation + flou gaussien + léger assombrissement :
    la plaque devient illisible même en zoomant, sans toucher la carrosserie.
    """
    h, w = arr_bgr.shape[:2]
    pad = max(2, min(8, int(box.h * 0.08)))
    x1, y1 = max(0, box.x1 - pad), max(0, box.y1 - pad)
    x2, y2 = min(w, box.x2 + pad), min(h, box.y2 + pad)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return
    strength = max(1, min(5, int(strength)))
    plate_h = y2 - y1
    block = max(5, int(round(plate_h / (8.0 - strength))))
    k = max(3, (plate_h // 4) | 1)

    roi = arr_bgr[y1:y2, x1:x2]
    rh, rw = roi.shape[:2]
    blurred = cv2.GaussianBlur(roi, (k, k), 0)
    small = cv2.resize(roi, (max(1, rw // block), max(1, rh // block)), interpolation=cv2.INTER_LINEAR)
    pixelated = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
    mixed = cv2.addWeighted(blurred, 0.5, pixelated, 0.5, 0)
    mixed = (mixed.astype(np.float32) * 0.94).astype(np.uint8)

    mask = cv2.GaussianBlur(np.full((rh, rw), 255, dtype=np.uint8), (0, 0), sigmaX=2.5)
    m = (mask.astype(np.float32) / 255.0)[..., np.newaxis]
    region = roi.astype(np.float32)
    blended = region * (1.0 - m) + mixed.astype(np.float32) * m
    arr_bgr[y1:y2, x1:x2] = blended.astype(np.uint8)
