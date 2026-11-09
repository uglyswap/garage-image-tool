"""API FastAPI du Garage Image Tool.

Endpoints principaux :
- POST /api/auth/login|logout       session par mot de passe (GET /api/auth/status)
- GET  /api/health                  état du service + moteur de plaques
- GET  /api/backgrounds             liste des fonds enregistrés
- POST /api/backgrounds             enregistre un nouveau fond
- DEL  /api/backgrounds/{id}        supprime un fond
- POST /api/jobs                    traite un lot d'images (bulk)
- GET  /api/jobs/{id}               progression + résultats
- GET  /api/results/{id}/{index}    fichier résultat (?download=1)
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shutil
import threading
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field, FiniteFloat

from . import auth as auth_module
from . import config
from .processing import (Layers, ProcessOptions, auto_adjust, clean_adjust, encode_image, foreground_stats,
                         load_layers, make_thumb, process_image, render_layers)
from .plate_detector import detector
from .store import BackgroundStore, JobStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
logger = logging.getLogger("garage-image-tool")

config.ensure_dirs()
backgrounds = BackgroundStore(config.BACKGROUND_DIR, os.path.join(config.BACKGROUND_DIR, "index.json"))
jobs = JobStore(config.JOBS_DIR, config.JOB_TTL_MINUTES)
executor = ThreadPoolExecutor(max_workers=config.MAX_WORKERS, thread_name_prefix="worker")
auth = auth_module.Auth.from_config()

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe_stem(name: str) -> str:
    stem = _SAFE_NAME.sub("_", Path(name).stem).strip() or "image"
    return stem[:60]


async def _read_limited(file: UploadFile, max_mb: int) -> bytes:
    limit = max_mb * 1024 * 1024
    buf = bytearray()
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(413, "Fichier trop volumineux (max " + str(max_mb) + " Mo)")
    if not buf:
        raise HTTPException(400, "Fichier vide")
    return bytes(buf)


class _UploadBudget:
    """Octets restants autorisés pour l ensemble d un lot."""

    def __init__(self, total_bytes: int):
        self.remaining = total_bytes


async def _stream_to_disk(file: UploadFile, dest: str, max_mb: int, budget: _UploadBudget) -> None:
    """Écrit l upload sur disque par blocs (jamais tout le lot en RAM)."""
    limit = max_mb * 1024 * 1024
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            budget.remaining -= len(chunk)
            if size > limit:
                raise HTTPException(413, "Fichier trop volumineux (max " + str(max_mb) + " Mo)")
            if budget.remaining < 0:
                raise HTTPException(413, "Lot trop volumineux (max " + str(config.MAX_TOTAL_MB) + " Mo au total)")
            out.write(chunk)
    if size == 0:
        raise HTTPException(400, "Fichier vide")


def _check_image(source, label: str) -> None:
    """Vérifie les octets réels : vrai JPEG/PNG/WebP, dimensions raisonnables."""
    try:
        with Image.open(source) as im:
            fmt = im.format
            pixels = im.width * im.height
            im.verify()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(415, "Image illisible ou corrompue : " + label) from exc
    if fmt not in ("JPEG", "PNG", "WEBP"):
        raise HTTPException(415, "Format non supporté : " + label)
    if pixels > config.MAX_PIXELS:
        raise HTTPException(413, "Image trop grande (" + str(pixels // 1_000_000) + " Mpx) : " + label)


class RetouchPayload(BaseModel):
    """Corps validé de la retouche manuelle (coordonnées en pixels pleine résolution)."""

    polygon: list[tuple[FiniteFloat, FiniteFloat]] = Field(min_length=3, max_length=64)
    strength: int = Field(3, ge=1, le=5)
    style: Literal["blur", "white"] = "blur"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    jobs.reload_from_disk()
    threading.Thread(target=jobs.cleanup_loop, daemon=True, name="cleanup").start()
    # Préchauffage : charge le modèle rembg dès le démarrage (évite ~20 s au 1er job)
    threading.Thread(target=lambda: _warm_rembg(), daemon=True, name="warm-rembg").start()
    threading.Thread(target=_resume_interrupted_jobs, daemon=True, name="resume").start()
    if not auth.enabled:
        logger.warning("APP_PASSWORD non défini : accès NON protégé (mode ouvert). "
                       "Définissez APP_PASSWORD pour activer l'authentification.")
    logger.info("Garage Image Tool v%s prêt, moteur plaques : %s", config.VERSION, detector.engine)
    yield


def _warm_rembg() -> None:
    try:
        from .processing import _get_rembg_session

        _get_rembg_session()
        logger.info("Préchauffage rembg terminé")
    except Exception:  # noqa: BLE001
        logger.exception("Préchauffage rembg échoué (réessaie au 1er job)")


def _resume_interrupted_jobs() -> None:
    """Re-soumet les traitements interrompus par un redémarrage du conteneur."""
    resumed = 0
    for job, index, in_path, opts_dict, _jid in jobs.take_resume():
        try:
            opts_dict = dict(opts_dict or {})
            bg_bytes = None
            if opts_dict.get("bg_mode") == "replace":
                bg = backgrounds.get(opts_dict.get("background_id", ""))
                if bg:
                    with open(backgrounds.path(bg), "rb") as f:
                        bg_bytes = f.read()
            clean = {k: v for k, v in opts_dict.items() if k != "background_id"}
            options = ProcessOptions(**clean, background=bg_bytes)
            with job.lock:
                job.results[index].status = "pending"
            executor.submit(_process_one, job, index, in_path, options)
            resumed += 1
        except Exception:  # noqa: BLE001
            logger.exception("Reprise du job %s image %d impossible", job.id, index)
            with job.lock:
                try:
                    job.results[index].status = "error"
                    job.results[index].error = "Reprise impossible après redémarrage"
                except Exception:  # noqa: BLE001
                    pass
    if resumed:
        logger.info("Traitements repris après redémarrage : %d", resumed)


app = FastAPI(title=config.APP_NAME, version=config.VERSION, lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
auth_module.install(app, auth)

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    if not request.url.path.startswith("/api/docs"):  # Swagger charge ses scripts depuis un CDN
        headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    if auth.is_secure(request):
        headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


# --------------------------------- Santé ---------------------------------
@app.get("/api/health")
def health(request: Request):
    if not auth.allows(request):
        return {"status": "ok", "version": config.VERSION, "auth_required": True}
    return {
        "status": "ok",
        "auth_required": auth.enabled,
        "app": config.APP_NAME,
        "version": config.VERSION,
        "plate_engine": detector.engine,
        "plate_model": detector.model_file,
        "max_files": config.MAX_FILES,
        "max_file_mb": config.MAX_FILE_MB,
        "max_total_mb": config.MAX_TOTAL_MB,
        "workers": config.MAX_WORKERS,
    }


# ------------------------------ Arrière-plans -----------------------------
@app.get("/api/backgrounds")
def list_backgrounds():
    return [{"id": b.id, "name": b.name, "url": "/api/backgrounds/" + b.id + "/file"}
            for b in backgrounds.list()]


@app.post("/api/backgrounds")
async def add_background(file: UploadFile = File(...), name: str = Form("", max_length=100)):
    if file.content_type not in config.ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, "Format non supporté (JPEG, PNG ou WebP attendu)")
    data = await _read_limited(file, config.MAX_FILE_MB)
    _check_image(io.BytesIO(data), str(file.filename))
    item = backgrounds.add(name, file.filename or "fond.jpg", data)
    return {"id": item.id, "name": item.name, "url": "/api/backgrounds/" + item.id + "/file"}


@app.delete("/api/backgrounds/{bg_id}")
def delete_background(bg_id: str):
    if not backgrounds.delete(bg_id):
        raise HTTPException(404, "Fond introuvable")
    return {"deleted": bg_id}


@app.get("/api/backgrounds/{bg_id}/file")
def background_file(bg_id: str):
    item = backgrounds.get(bg_id)
    if not item:
        raise HTTPException(404, "Fond introuvable")
    return FileResponse(backgrounds.path(item))


# ---------------------------------- Jobs ----------------------------------
@app.post("/api/jobs")
async def create_job(
    files: list[UploadFile] = File(...),
    bg_mode: str = Form("original"),
    background_id: str = Form(""),
    blur_enabled: bool = Form(True),
    blur_strength: int = Form(3, ge=1, le=5),
    out_format: str = Form("jpeg"),
    jpeg_quality: int = Form(92, ge=50, le=100),
    rembg_model: str = Form("birefnet-general-lite"),
    plate_style: str = Form("blur"),
):
    if not files:
        raise HTTPException(400, "Aucune image reçue")
    if len(files) > config.MAX_FILES:
        raise HTTPException(413, "Maximum " + str(config.MAX_FILES) + " images par lot")
    if bg_mode not in ("original", "replace", "transparent"):
        raise HTTPException(400, "Mode de fond invalide")
    if out_format not in ("jpeg", "png"):
        raise HTTPException(400, "Format de sortie invalide")
    if rembg_model not in config.REMBG_ALLOWED_MODELS:
        raise HTTPException(400, "Modèle de détourage invalide")
    if plate_style not in ("blur", "white"):
        raise HTTPException(400, "Style de plaque invalide")

    bg_bytes = None
    if bg_mode == "replace":
        bg = backgrounds.get(background_id)
        if not bg:
            raise HTTPException(400, "Sélectionnez un arrière-plan enregistré")
        with open(backgrounds.path(bg), "rb") as f:
            bg_bytes = f.read()

    for up in files:
        if up.content_type not in config.ALLOWED_CONTENT_TYPES:
            raise HTTPException(415, "Format non supporté : " + str(up.filename))

    opts = {
        "bg_mode": bg_mode, "background_id": background_id,
        "blur_enabled": blur_enabled, "blur_strength": blur_strength,
        "out_format": out_format, "jpeg_quality": jpeg_quality,
        "rembg_model": rembg_model, "plate_style": plate_style,
    }
    job = jobs.create([up.filename or "image" for up in files], opts=opts)
    budget = _UploadBudget(config.MAX_TOTAL_MB * 1024 * 1024)
    in_paths = []
    try:
        # Écriture en flux sur disque + contrôle des octets réels, avant tout traitement
        for i, up in enumerate(files):
            in_path = os.path.join(jobs.job_dir(job.id), "in_" + str(i))
            await _stream_to_disk(up, in_path, config.MAX_FILE_MB, budget)
            await asyncio.to_thread(_check_image, in_path, str(up.filename))
            in_paths.append(in_path)
    except BaseException:
        jobs.discard(job.id)  # lot refusé : rien ne doit rester sur disque
        raise
    for i, in_path in enumerate(in_paths):
        executor.submit(_process_one, job, i, in_path, ProcessOptions(
            bg_mode=bg_mode,
            background=bg_bytes,
            blur_enabled=blur_enabled,
            blur_strength=blur_strength,
            out_format=out_format,
            jpeg_quality=jpeg_quality,
            rembg_model=rembg_model,
            plate_style=plate_style,
        ))
    return {"job_id": job.id, "total": job.total}


# --------------------------- Rendu des résultats ---------------------------
FORMATS = {"jpeg": ("jpg", "image/jpeg"), "png": ("png", "image/png"), "webp": ("webp", "image/webp")}
MAX_EDITS = 40            # retouches manuelles par image
PREVIEW_SIDE = 1280       # plus grand côté de l aperçu des réglages
PREVIEW_CACHE = 8         # images maîtres réduites gardées en mémoire pour l aperçu
STATS_STEP = 8            # sous-échantillonnage des mesures du réglage automatique
_preview_lock = threading.Lock()
_preview_cache: OrderedDict = OrderedDict()


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _item_fmt(item) -> str:
    return "png" if item.out_file.endswith(".png") else "jpeg"


def _layers(job, item, max_side: int | None = None) -> Layers:
    """Calques du maître d un résultat. À appeler sous job.file_lock."""
    jd = jobs.job_dir(job.id)
    if not item.master_file:
        # Résultat produit avant la v1.3 : l image existante devient le maître
        name = "master_" + str(item.index) + os.path.splitext(item.out_file)[1]
        shutil.copyfile(os.path.join(jd, item.out_file), os.path.join(jd, name))
        item.master_file = name
    mask = _read(os.path.join(jd, item.mask_file)) if item.mask_file else None
    return load_layers(_read(os.path.join(jd, item.master_file)), mask, max_side)


def _render_item(job, item) -> None:
    """Recalcule le résultat et sa vignette depuis le maître. À appeler sous job.file_lock."""
    layers = _layers(job, item)
    if item.stats is None:
        item.stats = foreground_stats(layers, STATS_STEP)
    img = render_layers(layers, item.edits, item.adjust)
    jd = jobs.job_dir(job.id)
    quality = int(job.opts.get("jpeg_quality", 92))
    _write_atomic(os.path.join(jd, item.out_file), encode_image(img, _item_fmt(item), quality, layers.icc))
    thumb = "th_" + str(item.index) + ".webp"
    _write_atomic(os.path.join(jd, thumb), make_thumb(img))
    item.thumb_file = thumb
    item.rev += 1


def _preview_layers(job, item) -> Layers:
    key = (job.id, item.index)
    with _preview_lock:
        hit = _preview_cache.get(key)
        if hit is not None:
            _preview_cache.move_to_end(key)
            return hit
    with job.file_lock:
        layers = _layers(job, item, PREVIEW_SIDE)
    with _preview_lock:
        _preview_cache[key] = layers
        while len(_preview_cache) > PREVIEW_CACHE:
            _preview_cache.popitem(last=False)
    return layers


def _done_item(job_id: str, index: int):
    job = jobs.get(job_id)
    if not job or index < 0 or index >= len(job.results):
        raise HTTPException(404, "Résultat introuvable")
    item = job.results[index]
    if item.status != "done" or not item.out_file:
        raise HTTPException(404, "Résultat non prêt")
    return job, item


def _result_json(job, r) -> dict:
    return {
        "index": r.index,
        "name": r.out_name or r.name,
        "original": r.name,
        "status": r.status,
        "error": r.error,
        "plates": r.plates,
        "url": ("/api/results/" + job.id + "/" + str(r.index)) if r.status == "done" else None,
        "rev": r.rev,
        "adjust": clean_adjust(r.adjust),
        "edits": len(r.edits),
        # True : les réglages ne touchent que le véhicule détouré, jamais le fond
        "fg_only": bool(r.mask_file) or job.opts.get("bg_mode") == "transparent",
    }


def _disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "ignore").decode().replace('"', "") or "image"
    return 'attachment; filename="' + ascii_name + '"; ' + "filename*=UTF-8''" + quote(name)


def _export(job, item, fmt: str | None, quality: int, name: str = "") -> tuple[bytes | None, str, str, str]:
    """Résultat prêt à télécharger : (octets ou None si le fichier sert tel quel, chemin, nom, type MIME)."""
    path = os.path.join(jobs.job_dir(job.id), item.out_file)
    current = _item_fmt(item)
    fmt = fmt or current
    ext, media = FORMATS[fmt]
    base = item.out_name or "image.jpg"
    stem = _safe_stem(name) if name.strip() else os.path.splitext(base)[0]
    if fmt == current:
        return None, path, stem + "." + ext, media
    with Image.open(path) as im:
        im.load()
        data = encode_image(im, fmt, quality, im.info.get("icc_profile"))
    return data, path, stem + "." + ext, media


def _discard_input(in_path: str) -> None:
    try:
        os.remove(in_path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Suppression de l original impossible : %s", in_path)


def _process_one(job, index: int, in_path: str, opts: ProcessOptions) -> None:
    item = job.results[index]
    item.status = "processing"
    try:
        with open(in_path, "rb") as f:
            data = f.read()
        result = process_image(data, opts)
        ext = "png" if result.fmt == "png" else "jpg"
        jd = jobs.job_dir(job.id)
        master_file = "master_" + str(index) + "." + result.master_ext
        _write_atomic(os.path.join(jd, master_file), result.data)
        if result.mask:
            item.mask_file = "mask_" + str(index) + ".png"
            _write_atomic(os.path.join(jd, item.mask_file), result.mask)
        item.master_file = master_file
        item.out_file = "out_" + str(index) + "." + ext
        item.out_name = _safe_stem(item.name) + "_modifie." + ext
        with job.file_lock:
            _render_item(job, item)
        item.plates = result.plates_blurred
        item.status = "done"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Échec du traitement de l'image %d", index)
        item.status = "error"
        item.error = str(exc) or "Erreur inconnue"
    finally:
        _discard_input(in_path)  # RGPD : l original non flouté ne survit pas au traitement
        with job.lock:
            job.done += 1
            if job.done >= job.total:
                job.status = "done"
        jobs.save_status(job)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")
    with job.lock:
        return {
            "job_id": job.id,
            "status": job.status,
            "total": job.total,
            "done": job.done,
            "blur_enabled": job.opts.get("blur_enabled", True),
            "plate_style": job.opts.get("plate_style", "blur"),
            "bg_mode": job.opts.get("bg_mode", "original"),
            "results": [_result_json(job, r) for r in job.results],
        }


@app.get("/api/results/{job_id}/{index}")
def result_file(job_id: str, index: int, download: int = 0, thumb: int = 0,
                format: str | None = Query(None, pattern="^(jpeg|png|webp)$"),
                quality: int = Query(92, ge=50, le=100),
                name: str = Query("", max_length=120)):
    """Résultat, vignette (?thumb=1) ou export « enregistrer sous » (?format=&quality=&name=)."""
    job, item = _done_item(job_id, index)
    headers = {"Cache-Control": "private, no-cache"}  # toujours revalidé : une retouche change le fichier
    if thumb and item.thumb_file:
        thumb_path = os.path.join(jobs.job_dir(job_id), item.thumb_file)
        if os.path.isfile(thumb_path):
            return FileResponse(thumb_path, media_type="image/webp", headers=headers)
    data, path, filename, media = _export(job, item, format, quality, name)
    if download:
        headers["Content-Disposition"] = _disposition(filename)
    if data is None:
        return FileResponse(path, media_type=media, headers=headers)
    return Response(data, media_type=media, headers=headers)


class _ZipSink:
    """Destination non positionnable : zipfile y écrit en flux, sans fichier temporaire."""

    def __init__(self):
        self._chunks: list[bytes] = []

    def write(self, data) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks = []
        return out


@app.get("/api/jobs/{job_id}/zip")
def job_zip(job_id: str,
            indices: str = Query("", max_length=2000, pattern="^[0-9,]*$"),
            format: str | None = Query(None, pattern="^(jpeg|png|webp)$"),
            quality: int = Query(92, ge=50, le=100),
            name: str = Query("", max_length=80)):
    """Archive ZIP des résultats (tous, ou ?indices=0,2,5), éventuellement convertis."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")
    wanted = {int(x) for x in indices.split(",") if x}
    items = [r for r in job.results
             if r.status == "done" and r.out_file and (not wanted or r.index in wanted)]
    if not items:
        raise HTTPException(404, "Aucun résultat à télécharger")

    def stream():
        sink = _ZipSink()
        used: set[str] = set()
        # Sans compression : les images le sont déjà, le ZIP ne coûte presque aucun CPU
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as archive:
            for item in items:
                try:
                    data, path, filename, _media = _export(job, item, format, quality)
                    if data is None:
                        data = _read(path)
                except Exception:  # noqa: BLE001
                    logger.exception("ZIP : image %d ignorée", item.index)
                    continue
                stem, ext = os.path.splitext(filename)
                n = 2
                while filename.lower() in used:  # deux photos de même nom dans le lot
                    filename = stem + "_" + str(n) + ext
                    n += 1
                used.add(filename.lower())
                archive.writestr(filename, data)
                yield sink.take()
        yield sink.take()

    zip_name = _safe_stem(name if name.strip() else "photos_" + job.id) + ".zip"
    return StreamingResponse(stream(), media_type="application/zip",
                             headers={"Content-Disposition": _disposition(zip_name)})


# ------------------------- Réglages d image --------------------------------
class AdjustPayload(BaseModel):
    """Réglages de -100 à 100, appliqués au premier plan seulement quand le fond a été retiré."""

    brightness: int = Field(0, ge=-100, le=100)
    contrast: int = Field(0, ge=-100, le=100)
    saturation: int = Field(0, ge=-100, le=100)
    vibrance: int = Field(0, ge=-100, le=100)


def _rerender(job, item, rollback) -> dict:
    """Rendu après modification ; rollback() restaure l état précédent en cas d échec."""
    try:
        _render_item(job, item)
    except Exception as exc:  # noqa: BLE001
        rollback()
        logger.exception("Rendu du résultat %d impossible", item.index)
        raise HTTPException(500, "Rendu de l'image impossible") from exc
    return _result_json(job, item)


@app.put("/api/results/{job_id}/{index}/adjust")
def adjust_result(job_id: str, index: int, payload: AdjustPayload):
    job, item = _done_item(job_id, index)
    with job.file_lock:
        previous = dict(item.adjust)
        item.adjust = payload.model_dump()
        out = _rerender(job, item, lambda: setattr(item, "adjust", previous))
    jobs.save_status(job)
    return out


@app.get("/api/results/{job_id}/{index}/preview")
def preview_result(job_id: str, index: int,
                   brightness: int = Query(0, ge=-100, le=100),
                   contrast: int = Query(0, ge=-100, le=100),
                   saturation: int = Query(0, ge=-100, le=100),
                   vibrance: int = Query(0, ge=-100, le=100)):
    """Aperçu réduit des réglages, calculé par le même code que le rendu final."""
    job, item = _done_item(job_id, index)
    layers = _preview_layers(job, item)
    img = render_layers(layers, item.edits, {"brightness": brightness, "contrast": contrast,
                                             "saturation": saturation, "vibrance": vibrance})
    fmt = "webp" if img.mode == "RGBA" else "jpeg"
    return Response(encode_image(img, fmt, 85, layers.icc), media_type=FORMATS[fmt][1],
                    headers={"Cache-Control": "no-store"})


class AutoAdjustPayload(BaseModel):
    indices: list[int] | None = Field(None, max_length=500)


@app.post("/api/jobs/{job_id}/auto-adjust")
def auto_adjust_job(job_id: str, payload: AutoAdjustPayload):
    """Propose, pour chaque image, les réglages qui harmonisent le lot (rien n est appliqué ici)."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")
    wanted = set(payload.indices) if payload.indices else None
    items = [r for r in job.results
             if r.status == "done" and r.out_file and (wanted is None or r.index in wanted)]
    for item in items:
        if item.stats is None:  # résultat antérieur à la v1.3 : mesure à la demande
            with job.file_lock:
                item.stats = foreground_stats(_layers(job, item, 512))
    suggestions = auto_adjust([item.stats for item in items])
    return {"adjust": {str(item.index): adj for item, adj in zip(items, suggestions)}}


# ------------------------- Retouche manuelle ------------------------------
@app.post("/api/retouch/{job_id}/{index}")
def retouch_result(job_id: str, index: int, payload: RetouchPayload):
    """Masque l intérieur d un polygone dessiné par l utilisateur (coordonnées en pixels de l image
    pleine résolution). La retouche est mémorisée et rejouée sur le maître : elle reste annulable."""
    job, item = _done_item(job_id, index)
    pts = [[int(round(x)), int(round(y))] for x, y in payload.polygon]
    with job.file_lock:
        if len(item.edits) >= MAX_EDITS:
            raise HTTPException(409, "Trop de retouches sur cette image : annulez-en d'abord")
        item.edits.append({"polygon": pts, "strength": payload.strength, "style": payload.style})
        out = _rerender(job, item, item.edits.pop)
    jobs.save_status(job)
    return {"ok": True, **out}


@app.delete("/api/retouch/{job_id}/{index}")
def undo_retouch(job_id: str, index: int, all: int = 0):
    """Annule la dernière retouche manuelle (?all=1 : toutes)."""
    job, item = _done_item(job_id, index)
    with job.file_lock:
        if not item.edits:
            raise HTTPException(409, "Aucune retouche à annuler")
        previous = list(item.edits)
        item.edits = [] if all else previous[:-1]
        out = _rerender(job, item, lambda: setattr(item, "edits", previous))
    jobs.save_status(job)
    return {"ok": True, **out}


# ----------------------------- Frontend statique --------------------------
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
