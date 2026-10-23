"""Environnement de test : répertoires temporaires, aucun modèle chargé."""
import os
import tempfile

_ROOT = tempfile.mkdtemp(prefix="garage-tests-")
os.environ["DATA_DIR"] = os.path.join(_ROOT, "data")
os.environ["MODELS_DIR"] = os.path.join(_ROOT, "models")
os.environ["APP_PASSWORD"] = ""
