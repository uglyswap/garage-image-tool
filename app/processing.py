"""Pipeline de retouche : détourage rembg, fond de remplacement, flou des plaques.

Tout est 100% local (CPU) : rembg (ISNet/BiRefNet) pour le détourage, OpenCV
pour le flou, Pillow pour l encodage. Aucune API externe.
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


@dataclass
class ProcessOptions:
    bg_mode: str = "original"        # original | replace | transparent
    background: bytes | None = None
    blur_enabled: bool = True
    blur_strength: int = 3           # 1..5
    out_format: str = "jpeg"         # jpeg | png (forcé PNG si transparent)
    jpeg_quality: int = 92
    rembg_model: str = "birefnet-general-lite"
    plate_style: str = "blur"        # blur | white


@dataclass
class ProcessResult:
    data: bytes
    fmt: str                         # "jpeg" | "png"
    plates_blurred: int
    width: int
    height: int


def process_image(data: bytes, opts: ProcessOptions) -> ProcessResult:
    """Traite une image (bytes JPEG/PNG/WebP) et renvoie l image finale."""
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if image.mode != "RGB":
        image = image.convert("RGB")

    bgr = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    plates: list[PlateBox] = detector.detect(bgr) if opts.blur_enabled else []

    alpha_arr = None
    cutout = None
    dx, dy = 0, 0
    if opts.bg_mode in ("transparent", "replace"):
        cutout = _remove_background(image, opts.rembg_model)
        dx, dy = _bottom_align_offset(cutout)
        shifted = Image.new("RGBA", cutout.size, (0, 0, 0, 0))
        shifted.paste(cutout, (dx, dy), cutout)
        alpha_arr = np.asarray(shifted.getchannel("A"))
        cutout = shifted
        plates = [PlateBox(b.x1 + dx, b.y1 + dy, b.x2 + dx, b.y2 + dy, b.score) for b in plates]

    if opts.bg_mode == "transparent":
        out_img = cutout
    elif opts.bg_mode == "replace" and opts.background:
        bg = _load_background(opts.background, image.size)
        out_img = Image.alpha_composite(bg.convert("RGBA"), cutout)
    else:
        out_img = image.copy()

    blurred = 0
    if plates:
        rgb = np.asarray(out_img.convert("RGB"))
        bgr2 = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        for box in plates:
            if alpha_arr is not None:
                roi_alpha = alpha_arr[box.y1:box.y2, box.x1:box.x2]
                if roi_alpha.size and float(roi_alpha.mean()) < 30:
                    continue
            if opts.plate_style == "white":
                _white_plate(bgr2, box)
            else:
                _blur_plate(bgr2, box, opts.blur_strength)
            blurred += 1
        out_img = Image.fromarray(cv2.cvtColor(bgr2, cv2.COLOR_BGR2RGB))
        if alpha_arr is not None:
            out_img = out_img.convert("RGBA")
            out_img.putalpha(Image.fromarray(alpha_arr))

    fmt = "png" if (opts.out_format == "png" or opts.bg_mode == "transparent") else "jpeg"
    buf = io.BytesIO()
    if fmt == "jpeg":
        out_img.convert("RGB").save(buf, "JPEG", quality=opts.jpeg_quality, optimize=True, progressive=True)
    else:
        out_img.save(buf, "PNG", optimize=True)
    return ProcessResult(buf.getvalue(), fmt, blurred, image.width, image.height)


# ---------------------------------------------------------------------
def _remove_background(image: Image.Image, model: str = None) -> Image.Image:
    from rembg import remove

    model = model or config.REMBG_MODEL
    session = _get_session(model)
    if model.startswith("birefnet"):
        with _birefnet_gate:
            return _drop_small_islands(remove(image, session=session, post_process_mask=True))
    return _drop_small_islands(remove(image, session=session, post_process_mask=True))


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


def _bottom_align_offset(rgba: Image.Image):
    """Decalage pour poser le bas du vehicule sur le bas de l image, centre horizontalement."""
    a = np.array(rgba.getchannel("A"))
    ys, xs = np.nonzero(a > 25)
    W, H = rgba.size
    if len(ys) == 0:
        return 0, 0
    x1, x2 = int(xs.min()), int(xs.max())
    y2 = int(ys.max())
    car_w = x2 - x1 + 1
    dx = (W - car_w) // 2 - x1
    dy = (H - 1) - y2
    if car_w >= W:
        dx = -x1
    return dx, dy


def _load_background(bg_bytes: bytes, size: tuple[int, int]) -> Image.Image:
    """Charge le fond et le recadre en cover aux dimensions de l image."""
    bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
    tw, th = size
    sw, sh = bg.size
    scale = max(tw / sw, th / sh)
    nw, nh = max(tw, int(round(sw * scale))), max(th, int(round(sh * scale)))
    bg = bg.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return bg.crop((left, top, left + tw, top + th))


def _plate_mask(shape, box):
    """Masque de la plaque : quadrilatere incline si pts disponibles, sinon rectangle."""
    mask = np.zeros(shape[:2], dtype=np.uint8)
    h, w = shape[:2]
    if box.pts:
        pts = np.array(box.pts, dtype=np.float32)
        center = pts.mean(axis=0)
        expanded = center + (pts - center) * 1.02
        cv2.fillPoly(mask, [np.round(expanded).astype(np.int32)], 255)
    else:
        pad = max(1, min(3, int(box.h * 0.02)))
        cv2.rectangle(mask,
                      (max(0, box.x1 - pad), max(0, box.y1 - pad)),
                      (min(w, box.x2 + pad), min(h, box.y2 + pad)), 255, -1)
    return cv2.GaussianBlur(mask, (0, 0), sigmaX=2.5)


def _blur_plate(arr_bgr: np.ndarray, box: PlateBox, strength: int) -> None:
    """Floute uniquement l interieur de la plaque (masque suivant son inclinaison)."""
    mask = _plate_mask(arr_bgr.shape, box)
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    if x2 - x1 < 4 or y2 - y1 < 4:
        return
    strength = max(1, min(5, int(strength)))
    plate_h = max(8, y2 - y1)
    block = max(5, int(round(plate_h / (8.0 - strength))))
    k = max(3, (plate_h // 4) | 1)

    roi = arr_bgr[y1:y2, x1:x2]
    rh, rw = roi.shape[:2]
    blurred = cv2.GaussianBlur(roi, (k, k), 0)
    small = cv2.resize(roi, (max(1, rw // block), max(1, rh // block)), interpolation=cv2.INTER_LINEAR)
    pixelated = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
    mixed = cv2.addWeighted(blurred, 0.5, pixelated, 0.5, 0)
    mixed = (mixed.astype(np.float32) * 0.94).astype(np.uint8)

    m = (mask[y1:y2, x1:x2].astype(np.float32) / 255.0)[..., np.newaxis]
    region = roi.astype(np.float32)
    blended = region * (1.0 - m) + mixed.astype(np.float32) * m
    arr_bgr[y1:y2, x1:x2] = blended.astype(np.uint8)


def _white_plate(arr_bgr, box):
    """Remplace la plaque par un quadrilatere blanc epousant son inclinaison."""
    mask = _plate_mask(arr_bgr.shape, box)
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    m = (mask[y1:y2, x1:x2].astype(np.float32) / 255.0)[..., np.newaxis]
    white = np.full_like(arr_bgr[y1:y2, x1:x2], 250, dtype=np.float32)
    region = arr_bgr[y1:y2, x1:x2].astype(np.float32)
    blended = region * (1.0 - m) + white * m
    arr_bgr[y1:y2, x1:x2] = blended.astype(np.uint8)


def _blur_polygon(arr_bgr, pts, strength):
    """Flou (pixelisation + gaussien) a l interieur d un polygone, bords adoucis."""
    mask = np.zeros(arr_bgr.shape[:2], dtype=np.uint8)
    poly = np.array(pts, dtype=np.int32)
    cv2.fillPoly(mask, [poly], 255)
    mask = cv2.GaussianBlur(mask, (0, 0), sigmaX=3)
    m = (mask.astype(np.float32) / 255.0)[..., np.newaxis]
    xs = poly[:, 0]
    ys = poly[:, 1]
    pad = 8
    x1 = max(0, int(xs.min()) - pad)
    y1 = max(0, int(ys.min()) - pad)
    x2 = min(arr_bgr.shape[1], int(xs.max()) + pad)
    y2 = min(arr_bgr.shape[0], int(ys.max()) + pad)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return
    strength = max(1, min(5, int(strength)))
    bh = y2 - y1
    block = max(5, int(round(bh / (8.0 - strength))))
    k = max(3, (bh // 4) | 1)
    roi = arr_bgr[y1:y2, x1:x2]
    rh, rw = roi.shape[:2]
    blurred = cv2.GaussianBlur(roi, (k, k), 0)
    small = cv2.resize(roi, (max(1, rw // block), max(1, rh // block)), interpolation=cv2.INTER_LINEAR)
    pixelated = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
    mixed = cv2.addWeighted(blurred, 0.5, pixelated, 0.5, 0)
    mixed = (mixed.astype(np.float32) * 0.94).astype(np.uint8)
    sub = m[y1:y2, x1:x2]
    region = roi.astype(np.float32)
    blended = region * (1.0 - sub) + mixed.astype(np.float32) * sub
    arr_bgr[y1:y2, x1:x2] = blended.astype(np.uint8)


def retouch_bytes(data: bytes, pts, strength: int = 3, fmt: str = "jpeg", style: str = "blur"):
    """Applique la retouche polygonale (flou ou blanc) sur une image encodee."""
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("image illisible")
    if style == "white":
        poly = np.array([list(p) for p in pts], dtype=np.int32)
        cv2.fillPoly(img, [poly], (250, 250, 250))
    else:
        _blur_polygon(img, [list(p) for p in pts], strength)
    ext = ".jpg" if fmt == "jpeg" else ".png"
    params = [cv2.IMWRITE_JPEG_QUALITY, 92] if fmt == "jpeg" else []
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise ValueError("encodage impossible")
    return buf.tobytes(), fmt
