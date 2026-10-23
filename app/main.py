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
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field, FiniteFloat

from . import auth as auth_module
from . import config
from .processing import ProcessOptions, process_image, retouch_bytes
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
    reloaded = jobs.reload_from_disk()
    threading.Thread(target=jobs.cleanup_loop, daemon=True, name="cleanup").start()
    # Préchauffage : charge le modèle rembg dès le démarrage (évite ~20 s au 1er job)
    threading.Thread(target=lambda: _warm_rembg(), daemon=True, name="warm-rembg").start()
    threading.Thread(target=_resume_interrupted_jobs, daemon=True, name="resume").start()
    if not auth.enabled:
        logger.warning("APP_PASSWORD non défini : accès NON protégé (mode ouvert). "
                       "Définissez APP_PASSWORD pour activer l'authentification.")
    logger.info("Garage Image Tool v%s prêt — moteur plaques : %s", config.VERSION, detector.engine)
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
        out_name = _safe_stem(item.name) + "_modifie." + ext
        out_file = "out_" + str(index) + "." + ext
        with open(os.path.join(jobs.job_dir(job.id), out_file), "wb") as f:
            f.write(result.data)
        item.out_file = out_file
        item.out_name = out_name
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
            "results": [{
                "index": r.index,
                "name": r.out_name or r.name,
                "original": r.name,
                "status": r.status,
                "error": r.error,
                "plates": r.plates,
                "url": ("/api/results/" + job_id + "/" + str(r.index)) if r.status == "done" else None,
            } for r in job.results],
        }


@app.get("/api/results/{job_id}/{index}")
def result_file(job_id: str, index: int, download: int = 0):
    job = jobs.get(job_id)
    if not job or index < 0 or index >= len(job.results):
        raise HTTPException(404, "Résultat introuvable")
    item = job.results[index]
    if item.status != "done" or not item.out_file:
        raise HTTPException(404, "Résultat non prêt")
    path = os.path.join(jobs.job_dir(job_id), item.out_file)
    media = "image/png" if item.out_file.endswith(".png") else "image/jpeg"
    headers = {}
    if download:
        name = item.out_name or "image.jpg"
        ascii_name = name.encode("ascii", "ignore").decode() or "image.jpg"
        headers["Content-Disposition"] = (
            'attachment; filename="' + ascii_name + '"; ' + "filename*=UTF-8''" + quote(name)
        )
    return FileResponse(path, media_type=media, headers=headers)


# ------------------------- Retouche manuelle ------------------------------
@app.post("/api/retouch/{job_id}/{index}")
def retouch_result(job_id: str, index: int, payload: RetouchPayload):
    """Floute l interieur d un polygone dessine par l utilisateur (coordonnees
    en pixels de l image pleine resolution)."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")
    if index < 0 or index >= len(job.results):
        raise HTTPException(404, "Resultat introuvable")
    item = job.results[index]
    if item.status != "done" or not item.out_file:
        raise HTTPException(404, "Resultat non pret")
    pts = [[int(round(x)), int(round(y))] for x, y in payload.polygon]
    path = os.path.join(jobs.job_dir(job_id), item.out_file)
    fmt = "png" if item.out_file.endswith(".png") else "jpeg"
    with job.file_lock:  # une retouche a la fois par job (lecture + ecriture atomique)
        with open(path, "rb") as f:
            data = f.read()
        new_data, _ = retouch_bytes(data, pts, payload.strength, fmt, payload.style)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(new_data)
        os.replace(tmp, path)
    return {"ok": True, "url": "/api/results/" + job_id + "/" + str(index)}


# ----------------------------- Frontend statique --------------------------
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
