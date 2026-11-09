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
PLATE_MASK_MARGIN = 0.03  # marge du masque autour de la plaque, en part de sa hauteur
_birefnet_gate = threading.Semaphore(1)
MASTER_JPEG_QUALITY = 97
THUMB_SIDE = 480          # plus grand côté des vignettes
ADJUST_STRIP = 256        # réglages calculés par bandes : pic mémoire borné même sur une très grande image
ADJUST_KEYS = ("brightness", "contrast", "saturation", "vibrance")
LUMA = (0.299, 0.587, 0.114)
BRIGHTNESS_GAIN = 0.5     # +100 = x1,5
CONTRAST_GAIN = 0.6       # +100 = x1,6 autour du gris moyen
AUTO_STRENGTH = 0.6       # part de l écart au lot corrigée par le réglage automatique
AUTO_LIMIT = 0.35         # réglage automatique plafonné à +/-35


def _session_options():
    """Options ONNX Runtime : arène mémoire désactivée (la RAM est rendue après chaque image)
    et nombre de threads configurable (ORT_THREADS)."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    if config.ORT_DISABLE_ARENA:
        so.enable_cpu_mem_arena = False
        so.enable_mem_pattern = False
    if config.ORT_THREADS > 0:
        so.intra_op_num_threads = config.ORT_THREADS
        so.inter_op_num_threads = 1
    return so


def _new_rembg_session(model: str):
    """Crée la session rembg avec nos SessionOptions.

    Instancie directement la classe de session : la signature de rembg.new_session varie selon
    les versions (sess_opts en mot-clé en 2.0.86, absent en 2.0.69 où il est construit en interne)
    alors que BaseSession(model_name, sess_opts, ...) est stable."""
    from rembg.sessions import sessions_class

    for session_class in sessions_class:
        if session_class.name() == model:
            return session_class(model, _session_options())
    raise ValueError("Modèle rembg inconnu : " + model)


def _get_session(model):
    """Session rembg par modèle (lazy), repli u2net."""
    with _session_lock:
        if model not in _sessions:
            try:
                _sessions[model] = _new_rembg_session(model)
                logger.info("Session rembg %s initialisée", model)
            except Exception:  # noqa: BLE001
                logger.exception("Modèle %s indisponible, repli u2net", model)
                _sessions[model] = _new_rembg_session("u2net")
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
    data: bytes                      # image maître (plaques déjà masquées), base des retouches et réglages
    fmt: str                         # format de sortie demandé : "jpeg" | "png"
    plates_blurred: int
    width: int
    height: int
    mask: bytes | None = None        # masque du premier plan (PNG niveaux de gris), mode "replace" uniquement
    master_ext: str = "jpg"          # extension du fichier maître : "jpg" | "png"


def process_image(data: bytes, opts: ProcessOptions) -> ProcessResult:
    """Traite une image (bytes JPEG/PNG/WebP) et renvoie l image finale."""
    opened = Image.open(io.BytesIO(data))
    # Profil couleur conservé jusqu'à la sortie (sinon les photos hors sRGB changent de teinte)
    icc = opened.info.get("icc_profile") if opened.mode in ("RGB", "RGBA") else None
    image = ImageOps.exif_transpose(opened)
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
        plates = _shift_plates(plates, dx, dy, image.width, image.height)

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
    # Le maître est écrit une seule fois : retouches et réglages sont rejoués dessus à chaque rendu,
    # donc aucune perte par réencodages successifs. JPEG 97 sans sous-échantillonnage, ou PNG sans perte.
    buf = io.BytesIO()
    extra = {"icc_profile": icc} if icc else {}
    if fmt == "jpeg":
        out_img.convert("RGB").save(buf, "JPEG", quality=MASTER_JPEG_QUALITY, subsampling=0, **extra)
    else:
        out_img.save(buf, "PNG", compress_level=3, **extra)
    mask = None
    if opts.bg_mode == "replace" and alpha_arr is not None:
        mbuf = io.BytesIO()
        Image.fromarray(alpha_arr).save(mbuf, "PNG", compress_level=3)
        mask = mbuf.getvalue()
    return ProcessResult(buf.getvalue(), fmt, blurred, image.width, image.height,
                         mask=mask, master_ext="png" if fmt == "png" else "jpg")


# ---------------------------------------------------------------------
def _shift_plates(plates: list[PlateBox], dx: int, dy: int, width: int, height: int) -> list[PlateBox]:
    """Décale les plaques (en conservant leurs coins inclinés) et les borne à l image.

    Une plaque qui sort entièrement de l image, ou devient trop petite une fois
    recadrée, est ignorée : plus d indices négatifs dans les slicings numpy."""
    out: list[PlateBox] = []
    for b in plates:
        x1, y1 = max(0, b.x1 + dx), max(0, b.y1 + dy)
        x2, y2 = min(width, b.x2 + dx), min(height, b.y2 + dy)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        pts = [[p[0] + dx, p[1] + dy] for p in b.pts] if b.pts else None
        out.append(PlateBox(x1, y1, x2, y2, b.score, pts))
    return out


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
    if box.pts:
        cv2.fillPoly(mask, [np.round(np.array(box.pts, dtype=np.float32)).astype(np.int32)], 255)
    else:
        cv2.rectangle(mask, (box.x1, box.y1), (box.x2, box.y2), 255, -1)
    # Marge fine et uniforme autour de la plaque (3 % de sa hauteur), bord adouci au minimum
    r = max(1, int(round(PLATE_MASK_MARGIN * box.h)))
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    return cv2.GaussianBlur(mask, (0, 0), sigmaX=max(0.8, 0.25 * r + 0.6))


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


def _white_polygon(arr, pts):
    """Polygone blanc aux bords adoucis (même rendu que le masque automatique)."""
    poly = np.array(pts, dtype=np.int32)
    pad = 4
    x1 = max(0, int(poly[:, 0].min()) - pad)
    y1 = max(0, int(poly[:, 1].min()) - pad)
    x2 = min(arr.shape[1], int(poly[:, 0].max()) + pad + 1)
    y2 = min(arr.shape[0], int(poly[:, 1].max()) + pad + 1)
    if x2 <= x1 or y2 <= y1:
        return
    mask = np.zeros((y2 - y1, x2 - x1), dtype=np.uint8)
    cv2.fillPoly(mask, [poly - np.array([x1, y1], dtype=np.int32)], 255)
    m = (cv2.GaussianBlur(mask, (0, 0), sigmaX=1.0).astype(np.float32) / 255.0)[..., np.newaxis]
    region = arr[y1:y2, x1:x2].astype(np.float32)
    arr[y1:y2, x1:x2] = (region * (1.0 - m) + 250.0 * m).astype(np.uint8)


def retouch_bytes(data: bytes, pts, strength: int = 3, fmt: str = "jpeg", style: str = "blur"):
    """Applique la retouche polygonale (flou ou blanc) sur une image encodee.

    Le canal alpha d un PNG transparent est preserve (la retouche ne touche que les canaux couleur)."""
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("image illisible")
    alpha = None
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        alpha = np.ascontiguousarray(img[:, :, 3])
        img = np.ascontiguousarray(img[:, :, :3])
    h, w = img.shape[:2]
    poly_pts = [[min(max(int(p[0]), 0), w - 1), min(max(int(p[1]), 0), h - 1)] for p in pts]
    if style == "white":
        _white_polygon(img, poly_pts)
    else:
        _blur_polygon(img, poly_pts, strength)
    if alpha is not None:
        img = np.dstack([img, alpha])
    ext = ".jpg" if fmt == "jpeg" else ".png"
    params = [cv2.IMWRITE_JPEG_QUALITY, 92] if fmt == "jpeg" else []
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise ValueError("encodage impossible")
    return buf.tobytes(), fmt


# ------------------------- Rendu non destructif ---------------------------
@dataclass
class Layers:
    """Image maître décodée : couleurs, alpha éventuel, masque du premier plan, profil couleur."""
    rgb: np.ndarray                  # H x W x 3, uint8
    alpha: np.ndarray | None         # H x W, uint8 (PNG transparent)
    fg_mask: np.ndarray | None       # H x W, uint8 : zone concernée par les réglages (None = toute l image)
    icc: bytes | None
    scale: float                     # taille décodée / taille réelle (1.0 hors aperçu)


def load_layers(master: bytes, mask: bytes | None = None, max_side: int | None = None) -> Layers:
    """Décode le maître (et le masque), éventuellement réduit pour un aperçu rapide."""
    im = Image.open(io.BytesIO(master))
    icc = im.info.get("icc_profile")
    full_w = im.width
    if max_side and im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))  # décodage JPEG directement à taille réduite
    im = im.convert("RGBA" if (im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info) else "RGB")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.BILINEAR)
    arr = np.array(im)
    alpha = np.ascontiguousarray(arr[:, :, 3]) if arr.shape[2] == 4 else None
    rgb = np.ascontiguousarray(arr[:, :, :3])
    fg_mask = alpha
    if mask:
        m = Image.open(io.BytesIO(mask)).convert("L")
        if m.size != im.size:
            m = m.resize(im.size, Image.BILINEAR)
        fg_mask = np.array(m)
    return Layers(rgb, alpha, fg_mask, icc, im.width / float(full_w))


def clean_adjust(adjust: dict | None) -> dict:
    """Réglages bornés à -100..100, clés inconnues ignorées."""
    out = {}
    for key in ADJUST_KEYS:
        try:
            value = int(round(float((adjust or {}).get(key, 0))))
        except (TypeError, ValueError):
            value = 0
        out[key] = max(-100, min(100, value))
    return out


def _apply_adjust(rgb: np.ndarray, mask: np.ndarray | None, adjust: dict) -> None:
    """Luminosité, contraste, saturation, vibrance, en place.

    Avec un masque, seul le premier plan est modifié (fondu selon l opacité du masque) :
    l arrière-plan reste strictement identique."""
    adj = clean_adjust(adjust)
    b, c, s, v = (adj[k] / 100.0 for k in ADJUST_KEYS)
    if not (b or c or s or v):
        return
    y0, y1 = 0, rgb.shape[0]
    if mask is not None:
        rows = np.flatnonzero(mask.any(axis=1))
        if rows.size == 0:
            return
        y0, y1 = int(rows[0]), int(rows[-1]) + 1
    luma = np.array(LUMA, dtype=np.float32)
    for top in range(y0, y1, ADJUST_STRIP):
        rows = slice(top, min(top + ADJUST_STRIP, y1))
        src = rgb[rows].astype(np.float32) / 255.0
        out = src * (1.0 + BRIGHTNESS_GAIN * b) if b else src
        if c:
            out = (out - 0.5) * (1.0 + CONTRAST_GAIN * c) + 0.5
        if s or v:
            gray = (out @ luma)[..., np.newaxis]
            factor = 1.0 + s
            if v:
                # Vibrance : agit surtout sur les couleurs ternes, épargne les teintes déjà saturées
                mx, mn = out.max(axis=2, keepdims=True), out.min(axis=2, keepdims=True)
                sat = np.clip((mx - mn) / np.maximum(mx, 1e-4), 0.0, 1.0)
                factor = factor * (1.0 + v * (1.0 - sat))
            out = gray + (out - gray) * factor
        out = np.clip(out, 0.0, 1.0)
        if mask is not None:
            m = (mask[rows].astype(np.float32) / 255.0)[..., np.newaxis]
            out = src + (out - src) * m
        rgb[rows] = (out * 255.0 + 0.5).astype(np.uint8)


def _apply_edits(rgb: np.ndarray, edits: list | None, scale: float) -> None:
    h, w = rgb.shape[:2]
    for edit in edits or []:
        pts = [[min(max(int(round(p[0] * scale)), 0), w - 1),
                min(max(int(round(p[1] * scale)), 0), h - 1)] for p in edit.get("polygon", [])]
        if len(pts) < 3:
            continue
        if edit.get("style") == "white":
            _white_polygon(rgb, pts)
        else:
            _blur_polygon(rgb, pts, edit.get("strength", 3))


def render_layers(layers: Layers, edits: list | None, adjust: dict | None) -> Image.Image:
    """Rejoue les retouches manuelles puis les réglages sur une copie du maître."""
    rgb = layers.rgb.copy()
    _apply_edits(rgb, edits, layers.scale)
    _apply_adjust(rgb, layers.fg_mask, adjust or {})
    if layers.alpha is not None:
        return Image.fromarray(np.dstack([rgb, layers.alpha]))
    return Image.fromarray(rgb)


def encode_image(img: Image.Image, fmt: str, quality: int = 92, icc: bytes | None = None) -> bytes:
    """Encode en jpeg, png ou webp. Un JPEG ne porte pas de transparence : aplati sur blanc."""
    buf = io.BytesIO()
    extra = {"icc_profile": icc} if icc else {}
    if fmt == "jpeg":
        if img.mode == "RGBA":
            flat = Image.new("RGB", img.size, (255, 255, 255))
            flat.paste(img, mask=img.getchannel("A"))
            img = flat
        img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True, progressive=True, **extra)
    elif fmt == "webp":
        img.save(buf, "WEBP", quality=quality, method=4, **extra)
    elif fmt == "png":
        img.save(buf, "PNG", **extra)
    else:
        raise ValueError("format inconnu : " + str(fmt))
    return buf.getvalue()


def make_thumb(img: Image.Image) -> bytes:
    """Vignette WebP (conserve la transparence) pour la grille de résultats."""
    thumb = img.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.BILINEAR)
    buf = io.BytesIO()
    thumb.save(buf, "WEBP", quality=80, method=2)
    return buf.getvalue()


# ------------------------- Réglage automatique ----------------------------
def foreground_stats(layers: Layers, step: int = 1) -> dict | None:
    """Mesures du premier plan : luminosité médiane, étendue tonale, saturation moyenne.

    step > 1 : un pixel sur step dans chaque direction (mesure rapide sur une grande image)."""
    rgb = layers.rgb[::step, ::step].astype(np.float32) / 255.0
    if layers.fg_mask is not None:
        rgb = rgb[layers.fg_mask[::step, ::step] > 127]
    else:
        rgb = rgb.reshape(-1, 3)
    if rgb.shape[0] < 50:
        return None
    lum = rgb @ np.array(LUMA, dtype=np.float32)
    p5, p50, p95 = (float(x) for x in np.percentile(lum, (5, 50, 95)))
    mx, mn = rgb.max(axis=1), rgb.min(axis=1)
    return {"lum": p50, "spread": max(p95 - p5, 1e-3), "sat": float((mx - mn).mean())}


def auto_adjust(stats: list[dict | None]) -> list[dict]:
    """Réglages qui rapprochent chaque image de la médiane du lot, pour un ensemble cohérent.

    Correction volontairement partielle et plafonnée : une voiture noire et une voiture blanche
    ne doivent pas finir de la même teinte, seul l écart d exposition entre photos est réduit."""
    valid = [s for s in stats if s]
    if not valid:
        return [clean_adjust(None) for _ in stats]
    target = {k: float(np.median([s[k] for s in valid])) for k in ("lum", "spread", "sat")}
    # Un lot uniformément sombre ou terne est aussi ramené vers une exposition correcte
    target["lum"] = 0.5 * target["lum"] + 0.5 * min(max(target["lum"], 0.42), 0.55)
    target["spread"] = max(target["spread"], 0.55)
    out = []
    for s in stats:
        if not s:
            out.append(clean_adjust(None))
            continue
        gain = 1.0 + AUTO_STRENGTH * (target["spread"] / s["spread"] - 1.0)
        c = min(max((gain - 1.0) / CONTRAST_GAIN, -AUTO_LIMIT), AUTO_LIMIT)
        gain = 1.0 + CONTRAST_GAIN * c
        wanted = s["lum"] + AUTO_STRENGTH * (target["lum"] - s["lum"])
        before_contrast = (wanted - 0.5) / gain + 0.5
        b = min(max((before_contrast / max(s["lum"], 0.02) - 1.0) / BRIGHTNESS_GAIN, -AUTO_LIMIT), AUTO_LIMIT)
        sat = 0.0
        if s["sat"] > 0.06 and target["sat"] > 0.06:  # carrosseries grises, blanches, noires : couleur intacte
            sat = min(max(AUTO_STRENGTH * (target["sat"] / s["sat"] - 1.0), -AUTO_LIMIT), AUTO_LIMIT)
        out.append(clean_adjust({"brightness": b * 100, "contrast": c * 100,
                                 "saturation": 0, "vibrance": sat * 100}))
    return out
