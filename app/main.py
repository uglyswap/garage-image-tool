"""API FastAPI du Garage Image Tool.

Endpoints principaux :
- GET  /api/health                  état du service + moteur de plaques
- GET  /api/backgrounds             liste des fonds enregistrés
- POST /api/backgrounds             enregistre un nouveau fond
- DEL  /api/backgrounds/{id}        supprime un fond
- POST /api/jobs                    traite un lot d'images (bulk)
- GET  /api/jobs/{id}               progression + résultats
- GET  /api/results/{id}/{index}    fichier résultat (?download=1)
"""
from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    reloaded = jobs.reload_from_disk()
    threading.Thread(target=jobs.cleanup_loop, daemon=True, name="cleanup").start()
    # Préchauffage : charge le modèle rembg dès le démarrage (évite ~20 s au 1er job)
    threading.Thread(target=lambda: _warm_rembg(), daemon=True, name="warm-rembg").start()
    logger.info("Garage Image Tool v%s prêt — moteur plaques : %s", config.VERSION, detector.engine)
    yield


def _warm_rembg() -> None:
    try:
        from .processing import _get_rembg_session

        _get_rembg_session()
        logger.info("Préchauffage rembg terminé")
    except Exception:  # noqa: BLE001
        logger.exception("Préchauffage rembg échoué (réessaie au 1er job)")


app = FastAPI(title=config.APP_NAME, version=config.VERSION, lifespan=lifespan,
              docs_url="/api/docs", redoc_url=None)


# --------------------------------- Santé ---------------------------------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "app": config.APP_NAME,
        "version": config.VERSION,
        "plate_engine": detector.engine,
        "plate_model": detector.model_file,
        "max_files": config.MAX_FILES,
        "max_file_mb": config.MAX_FILE_MB,
        "workers": config.MAX_WORKERS,
    }


# ------------------------------ Arrière-plans -----------------------------
@app.get("/api/backgrounds")
def list_backgrounds():
    return [{"id": b.id, "name": b.name, "url": "/api/backgrounds/" + b.id + "/file"}
            for b in backgrounds.list()]


@app.post("/api/backgrounds")
async def add_background(file: UploadFile = File(...), name: str = Form("")):
    if file.content_type not in config.ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, "Format non supporté (JPEG, PNG ou WebP attendu)")
    data = await _read_limited(file, config.MAX_FILE_MB)
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
    blur_strength: int = Form(3),
    out_format: str = Form("jpeg"),
    jpeg_quality: int = Form(92),
    rembg_model: str = Form("birefnet-general-lite"),
):
    if not files:
        raise HTTPException(400, "Aucune image reçue")
    if len(files) > config.MAX_FILES:
        raise HTTPException(413, "Maximum " + str(config.MAX_FILES) + " images par lot")
    if bg_mode not in ("original", "replace", "transparent"):
        raise HTTPException(400, "Mode de fond invalide")
    if out_format not in ("jpeg", "png"):
        raise HTTPException(400, "Format de sortie invalide")
    if rembg_model not in (config.REMBG_MODEL, config.REMBG_MAX_MODEL):
        raise HTTPException(400, "Modèle de détourage invalide")

    bg_bytes = None
    if bg_mode == "replace":
        bg = backgrounds.get(background_id)
        if not bg:
            raise HTTPException(400, "Sélectionnez un arrière-plan enregistré")
        with open(backgrounds.path(bg), "rb") as f:
            bg_bytes = f.read()

    # Lecture + validation de tous les fichiers avant création du job
    payloads = []
    for up in files:
        if up.content_type not in config.ALLOWED_CONTENT_TYPES:
            raise HTTPException(415, "Format non supporté : " + str(up.filename))
        payloads.append((up.filename or "image", await _read_limited(up, config.MAX_FILE_MB)))

    job = jobs.create([name for name, _ in payloads])
    jobs.save_status(job)
    for i, (_name, data) in enumerate(payloads):
        in_path = os.path.join(jobs.job_dir(job.id), "in_" + str(i))
        with open(in_path, "wb") as fh:
            fh.write(data)
        executor.submit(_process_one, job, i, in_path, ProcessOptions(
            bg_mode=bg_mode,
            background=bg_bytes,
            blur_enabled=blur_enabled,
            blur_strength=blur_strength,
            out_format=out_format,
            jpeg_quality=jpeg_quality,
            rembg_model=rembg_model,
        ))
    return {"job_id": job.id, "total": job.total}


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
            'attachment; filename="' + ascii_name + '"; ' + "filename*=UTF-8'" + quote(name)
        )
    return FileResponse(path, media_type=media, headers=headers)


# ------------------------- Retouche manuelle ------------------------------
@app.post("/api/retouch/{job_id}/{index}")
def retouch_result(job_id: str, index: int, payload: dict = Body(...)):
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
    polygon = payload.get("polygon") or []
    pts = []
    for p in polygon:
        try:
            pts.append([int(round(float(p[0]))), int(round(float(p[1])))])
        except Exception:  # noqa: BLE001
            continue
    if len(pts) < 3:
        raise HTTPException(400, "Polygone invalide (3 points minimum)")
    if len(pts) > 64:
        raise HTTPException(400, "Trop de points (64 maximum)")
    try:
        strength = max(1, min(5, int(payload.get("strength", 3))))
    except Exception:  # noqa: BLE001
        strength = 3
    path = os.path.join(jobs.job_dir(job_id), item.out_file)
    with open(path, "rb") as f:
        data = f.read()
    fmt = "png" if item.out_file.endswith(".png") else "jpeg"
    new_data, _ = retouch_bytes(data, pts, strength, fmt)
    with open(path, "wb") as f:
        f.write(new_data)
    return {"ok": True, "url": "/api/results/" + job_id + "/" + str(index)}


# ----------------------------- Frontend statique --------------------------
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
