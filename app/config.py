"""Configuration centrale (variables d'environnement surchargeables)."""
import os

APP_NAME = "Garage Image Tool"
VERSION = "1.0.0"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "app", "static")

MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(BASE_DIR, "models"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
BACKGROUND_DIR = os.path.join(DATA_DIR, "backgrounds")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
PLATE_MODEL_PATH = os.environ.get("PLATE_MODEL_PATH", os.path.join(MODELS_DIR, "plate_yolov8.onnx"))

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "2"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "25"))
MAX_FILES = int(os.environ.get("MAX_FILES", "60"))
JOB_TTL_MINUTES = int(os.environ.get("JOB_TTL_MINUTES", "1440"))  # resultats conserves 24h
PLATE_CONF_THRESHOLD = float(os.environ.get("PLATE_CONF_THRESHOLD", "0.35"))
REMBG_MODEL = os.environ.get("REMBG_MODEL", "birefnet-general-lite")  # isnet = option rapide
REMBG_MAX_MODEL = os.environ.get("REMBG_MAX_MODEL", "birefnet-general-lite")  # qualite Max (≈ remove.bg)
PLATE_SMALL_PASS = os.environ.get("PLATE_SMALL_PASS", "1") == "1"  # 2e passe tuiles x2 (petites plaques)
PLATE_CLASSIC_PASS = os.environ.get("PLATE_CLASSIC_PASS", "0") == "1"  # expérimental: sauvetage classique (FP sur carrosserie, désactivé par défaut)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def ensure_dirs() -> None:
    for d in (DATA_DIR, BACKGROUND_DIR, JOBS_DIR):
        os.makedirs(d, exist_ok=True)
