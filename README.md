# 🛠️ Garage Image Tool

**Retouche photo pour garages automobiles** — détourage automatique des véhicules, remplacement du fond, floutage des plaques d'immatriculation.

100 % Python · 100 % local (aucune API externe) · Un seul prérequis : **Docker**.

---

## ✨ Fonctionnalités

- 📤 **Upload en masse** — glissez-déposez jusqu'à 60 images (JPEG / PNG / WebP, 25 Mo max par image)
- ✂️ **Détourage automatique** du véhicule avec rembg (U²-Net, inférence CPU locale)
- 🖼️ **Bibliothèque d'arrière-plans** — enregistrez vos fonds (showroom, extérieur, fond couleur…) et sélectionnez celui que vous voulez ; le fond est recadré en mode *cover*. Mode **PNG transparent** également disponible.
- 🔒 **Floutage des plaques d'immatriculation** — détection YOLOv8 **pays-agnostique** (UE, US, Asie, motos…) puis affinage par contours : **seule la plaque est floutée**, jamais la moitié de la voiture. Pixelisation + flou gaussien = illisible même en zoomant.
- ⚡ **Traitement bulk parallèle** avec progression en direct image par image
- ⬇️ **Téléchargement en un clic** de toutes les images traitées (fichiers individuels téléchargés en rafale contrôlée — pas d'archive ZIP)
- 🧹 **Purge automatique** des résultats après 2 h — rien ne traîne sur le serveur
- 🇫🇷 Interface web en français, responsive, zéro dépendance JS (vanilla)
- 📚 **API REST** documentée (FastAPI / Swagger) pour intégration

---

## 🚀 Déploiement express

### Avec Docker Compose (recommandé)

```bash
git clone https://github.com/uglyswap/garage-image-tool.git
cd garage-image-tool
docker compose up -d --build
```

L'outil est disponible sur **http://localhost:8000** — c'est tout.

### Avec Docker seul

```bash
docker build -t garage-image-tool .
docker run -d --name garage-image-tool \
  -p 8000:8000 \
  -v garage-data:/data \
  --restart unless-stopped \
  garage-image-tool:latest
```

> 💡 Les modèles IA (~190 Mo au total) sont **téléchargés automatiquement pendant le build**. Aucune clé, aucun compte, aucune configuration nécessaire. Le volume `garage-data` conserve vos arrière-plans enregistrés entre les redémarrages.

---

## 🧠 Comment ça marche

1. **Détection des plaques** — un YOLOv8 exporté en ONNX (inférence `onnxruntime`, 100 % CPU) localise les plaques. Le modèle détecte la *géométrie* du rectangle de plaque, pas son texte : il fonctionne donc quel que soit le pays (plaques européennes, américaines, asiatiques, motos…). Un **affinage par contours** resserre ensuite la boîte sur la plaque réelle.
2. **Floutage** — pixelisation + flou gaussien combinés, appliqués **uniquement à l'intérieur de la plaque** via un masque aux bords adoucis (dilatation ≤ 8 px). Léger assombrissement pour tuer les reflets lisibles.
3. **Détourage** — rembg (réseau U²-Net) isole le véhicule avec un canal alpha nettoyé (`post_process_mask`).
4. **Fusion** — le véhicule est posé sur l'arrière-plan choisi (recadré *cover* aux dimensions d'origine) ou exporté sur fond transparent (PNG).

> 🔁 **Repli automatique** : si le modèle ONNX de plaques est absent ou corrompu, une cascade de Haar OpenCV prend le relais (précision moindre, plaques de type européen). L'état du moteur est visible en haut de l'interface et sur `/api/health`.

---

## ⚙️ Configuration

Variables d'environnement (toutes optionnelles) :

| Variable | Défaut | Description |
|---|---|---|
| `MAX_WORKERS` | `2` | Workers CPU parallèles (rembg / détection) |
| `MAX_FILE_MB` | `25` | Taille maximale par image |
| `MAX_FILES` | `60` | Nombre maximal d'images par lot |
| `JOB_TTL_MINUTES` | `120` | Durée de conservation des résultats avant purge |
| `PLATE_CONF_THRESHOLD` | `0.35` | Seuil de confiance de la détection de plaques |
| `PLATE_MODEL_PATH` | `/app/models/plate_yolov8.onnx` | Chemin du détecteur YOLO ONNX |
| `DATA_DIR` | `/data` | Données persistantes (fonds + jobs) |
| `MODELS_DIR` | `/app/models` | Emplacement des modèles |

---

## 🌐 API

La documentation Swagger interactive est disponible sur **`/api/docs`**.

| Méthode | Route | Description |
|---|---|---|
| GET | `/api/health` | État du service + moteur de plaques actif |
| GET | `/api/backgrounds` | Liste des arrière-plans enregistrés |
| POST | `/api/backgrounds` | Enregistre un nouveau fond (multipart `file`, `name`) |
| DELETE | `/api/backgrounds/{id}` | Supprime un fond |
| POST | `/api/jobs` | Traite un lot d'images (multipart) |
| GET | `/api/jobs/{id}` | Progression + résultats du job |
| GET | `/api/results/{id}/{index}?download=1` | Télécharge un résultat |

Exemple — traiter deux images avec remplacement de fond :

```bash
JOB=$(curl -s -X POST http://localhost:8000/api/jobs \
  -F "files=@photo1.jpg" \
  -F "files=@photo2.jpg" \
  -F "bg_mode=replace" \
  -F "background_id=abc123def456" \
  -F "blur_enabled=true" \
  -F "blur_strength=3" | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")

curl -s http://localhost:8000/api/jobs/$JOB
```

---

## 📦 Modèles embarqués

| Modèle | Taille | Rôle | Source |
|---|---|---|---|
| `u2net.onnx` | ~176 Mo | Détourage (segmentation U²-Net) | Releases officielles [rembg](https://github.com/danielgatis/rembg) |
| `plate_yolov8.onnx` | ~38 Mo | Détection de plaques (YOLO11s) | [morsetechlab/yolov11-license-plate-detection](https://huggingface.co/morsetechlab/yolov11-license-plate-detection) (Hugging Face, ~72k téléchargements) — repli auto : [ml-debi/yolov8-license-plate-detection](https://huggingface.co/ml-debi/yolov8-license-plate-detection) |

Les modèles sont téléchargés au **build** (script `scripts/download_models.py`, idempotent) puis embarqués dans l'image.

**Utiliser votre propre détecteur** : tout YOLOv8/YOLO11 de détection exporté ONNX (entrée 640×640, une classe) fonctionne. Montez le fichier dans le conteneur et pointez `PLATE_MODEL_PATH` dessus.

---

## 🧪 Développement

```
garage-image-tool/
├── app/
│   ├── main.py            # API FastAPI
│   ├── processing.py      # Pipeline : détourage + fond + flou
│   ├── plate_detector.py  # YOLO ONNX + repli Haar + affinage contours
│   ├── store.py           # Persistance fonds / jobs
│   ├── config.py          # Configuration
│   └── static/            # Frontend (HTML/CSS/JS vanilla)
├── scripts/
│   └── download_models.py # Téléchargement des modèles (build)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

Lancer hors Docker (Python 3.11) :

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=./data MODELS_DIR=./models
python scripts/download_models.py   # une seule fois
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## ⚠️ Limites connues

- Plaques **très petits ou très angulées** (photo lointaine, vue arrière en contre-plongeon extrême) : la détection peut échouer. Vérifiez le badge « plaques floutées » par image dans l'interface.
- Le détourage U²-Net peut occasionnellement accrocher des reflets complexes (vitres très teintées) — rare sur des photos de véhicule standard.
- En mode repli Haar (sans modèle ONNX), la couverture pays est réduite.
- Téléchargement multiple : Chrome/Firefox demandent une autorisation unique « télécharger plusieurs fichiers ». Safari/iOS peuvent limiter le nombre de téléchargements consécutifs.

## 🔐 RGPD / Confidentialité

- Les images sont traitées **intégralement sur votre serveur** : aucune donnée n'est envoyée à un service tiers, aucun appel réseau pendant le traitement.
- Les résultats sont stockés temporairement puis **purgés automatiquement** (2 h par défaut).
- Le floutage des plaques vise la conformité RGPD pour la publication de photos de véhicules (données personnelles = plaques + visages éventuels ; pensez aussi aux visages si présents).

## 📄 Licence

MIT — voir [LICENSE](LICENSE).
