# --------------------------------------------------------------------------
# Garage Image Tool - image production CPU
# Build : docker build -t garage-image-tool .
# Run   : docker run -d -p 8000:8000 -v garage-data:/data garage-image-tool
# --------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    U2NET_HOME=/app/models \
    MODELS_DIR=/app/models \
    DATA_DIR=/data

# Dependance systeme minimale pour OpenCV headless + onnxruntime
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Couche dependances (bien cachee par Docker)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Code + scripts de telechargement des modeles
COPY scripts/ scripts/
# Modeles embarques au build : u2net (~176 Mo) + plaques YOLOv8 ONNX (~12 Mo)
# (avant COPY app : un changement de code ne rettelecharge pas les modeles)
RUN python scripts/download_models.py
COPY app/ app/

# Utilisateur non-root
RUN useradd --create-home app \
 && mkdir -p /data \
 && chown -R app:app /app /data
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
