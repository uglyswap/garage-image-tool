"""Configuration centrale (variables d'environnement surchargeables)."""
import os

APP_NAME = "Garage Image Tool"
VERSION = "1.2.0"

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
MAX_TOTAL_MB = int(os.environ.get("MAX_TOTAL_MB", "400"))  # plafond cumule par lot
MAX_PIXELS = int(os.environ.get("MAX_PIXELS", "120000000"))  # anti decompression-bomb
JOB_TTL_MINUTES = int(os.environ.get("JOB_TTL_MINUTES", "1440"))  # resultats conserves 24h
PLATE_CONF_THRESHOLD = float(os.environ.get("PLATE_CONF_THRESHOLD", "0.30"))
REMBG_MODEL = os.environ.get("REMBG_MODEL", "birefnet-general-lite")  # isnet = option rapide
REMBG_MAX_MODEL = os.environ.get("REMBG_MAX_MODEL", "birefnet-general-lite")  # qualite Max (≈ remove.bg)
REMBG_FAST_MODEL = os.environ.get("REMBG_FAST_MODEL", "isnet-general-use")  # option "Rapide" de l'UI
REMBG_ALLOWED_MODELS = {REMBG_MODEL, REMBG_MAX_MODEL, REMBG_FAST_MODEL}
# ONNX Runtime : l'arene memoire garde ~7 Go occupes apres un detourage BiRefNet (mesure) ; desactivee,
# la memoire est rendue apres chaque image (pic inchange ~6 Go). ORT_THREADS=0 : valeur par defaut d'ORT.
ORT_DISABLE_ARENA = os.environ.get("ORT_DISABLE_ARENA", "1") == "1"
ORT_THREADS = int(os.environ.get("ORT_THREADS", "0"))
MAX_RESUMES = int(os.environ.get("MAX_RESUMES", "2"))  # reprises auto max d'un meme traitement interrompu
PLATE_WEAK_THRESHOLD = float(os.environ.get("PLATE_WEAK_THRESHOLD", "0.2"))  # detections faibles, retenues si contour de plaque net (0 = off)
PLATE_SMALL_PASS = os.environ.get("PLATE_SMALL_PASS", "1") == "1"  # 2e passe tuiles x2 (petites plaques)
PLATE_CLASSIC_PASS = os.environ.get("PLATE_CLASSIC_PASS", "0") == "1"  # expérimental: sauvetage classique (FP sur carrosserie, désactivé par défaut)

# --- Authentification (mot de passe unique partagé) ---
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # vide = mode ouvert (avertissement)
APP_SECRET = os.environ.get("APP_SECRET", "")  # vide = secret généré dans DATA_DIR/.secret
SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))
TRUSTED_PROXY = os.environ.get("TRUSTED_PROXY", "0") == "1"  # lit X-Forwarded-For / -Proto
SECRET_FILE = os.path.join(DATA_DIR, ".secret")

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def ensure_dirs() -> None:
    for d in (DATA_DIR, BACKGROUND_DIR, JOBS_DIR):
        os.makedirs(d, exist_ok=True)
