# 🛠️ Garage Image Tool

**Retouche photo pour garages automobiles** : détourage automatique des véhicules, remplacement du fond, floutage des plaques d'immatriculation.

100 % Python · 100 % local (aucune API externe) · Un seul prérequis : **Docker**.

---

## ✨ Fonctionnalités

- 📤 **Upload en masse** : glissez-déposez jusqu'à 60 images (JPEG / PNG / WebP, 25 Mo max par image)
- 🔐 **Authentification** par mot de passe unique (`APP_PASSWORD`) : écran de connexion, session sécurisée, accès API par `Bearer`
- ✂️ **Détourage automatique** du véhicule avec rembg : BiRefNet (qualité maximale, par défaut) ou ISNet (rapide), inférence CPU locale
- ✏️ **Retouche manuelle** d'une plaque oubliée : polygone dessiné à la souris, flou ou rectangle blanc
- 🖼️ **Bibliothèque d'arrière-plans** : enregistrez vos fonds (showroom, extérieur, fond couleur…) et sélectionnez celui que vous voulez ; le fond est recadré en mode *cover*. Mode **PNG transparent** également disponible.
- 🔒 **Masquage des plaques d'immatriculation** (flou ou rectangle blanc) : détection YOLOv8 **pays-agnostique** (UE, US, Asie, motos…) puis ajustement d'un **quadrilatère exact** (incliné, en perspective) : **seule la plaque est masquée**, jamais la moitié de la voiture. Pixelisation + flou gaussien = illisible même en zoomant.
- ⚡ **Traitement bulk parallèle** avec progression en direct image par image
- ⬇️ **Téléchargement en un clic** de toutes les images traitées (fichiers individuels téléchargés en rafale contrôlée : pas d'archive ZIP)
- 🧹 **Purge automatique** : les photos originales (non floutées) sont supprimées dès la fin du traitement, les résultats après 24 h (`JOB_TTL_MINUTES`)
- 🇫🇷 Interface web en français, responsive, zéro dépendance JS (vanilla)
- 📚 **API REST** documentée (FastAPI / Swagger) pour intégration

---

## 🚀 Déploiement express

### Avec Docker Compose (recommandé)

```bash
git clone https://github.com/uglyswap/garage-image-tool.git
cd garage-image-tool
echo "APP_PASSWORD=choisissez-un-mot-de-passe-solide" > .env
docker compose up -d --build
```

L'outil est disponible sur **http://localhost:8000** : connectez-vous avec le mot de passe défini dans `.env`.

> ⚠️ **Sans `APP_PASSWORD`, l'accès n'est pas protégé** : l'application démarre quand même (compatibilité), écrit un avertissement dans les logs et affiche un bandeau rouge dans l'interface. Ne l'exposez jamais sur Internet dans cet état.

### Avec Docker seul

```bash
docker build -t garage-image-tool .
docker run -d --name garage-image-tool \
  -p 8000:8000 \
  -e APP_PASSWORD='choisissez-un-mot-de-passe-solide' \
  -v garage-data:/data \
  --restart unless-stopped \
  garage-image-tool:latest
```

> 💡 Les modèles IA (~630 Mo au total) sont **téléchargés automatiquement pendant le build** et vérifiés par SHA-256. Aucune clé ni compte nécessaire. Le volume `garage-data` conserve vos arrière-plans, vos résultats et le secret de session entre les redémarrages.

---

## 🧠 Comment ça marche

1. **Détection des plaques** : un YOLOv8 exporté en ONNX (inférence `onnxruntime`, 100 % CPU) localise les plaques. Le modèle détecte la *géométrie* du rectangle de plaque, pas son texte : il fonctionne donc quel que soit le pays (plaques européennes, américaines, asiatiques, motos…). Un **ajustement par contour fermé** cherche ensuite, dans la boîte, le quadrilatère à 4 coins de la plaque (incliné ou en trapèze). À défaut, la boîte est réduite de 3 % plutôt qu'agrandie.
2. **Masquage** : pixelisation + flou gaussien combinés (ou rectangle blanc), appliqués **uniquement à l'intérieur de la plaque** via un masque aux bords adoucis (marge de 3 % de la hauteur de la plaque). Léger assombrissement pour tuer les reflets lisibles.
3. **Détourage** : rembg isole le véhicule avec un canal alpha nettoyé (`post_process_mask`, suppression des îlots flottants). Modèle BiRefNet par défaut (qualité maximale) ou ISNet (rapide) ; U²-Net est embarqué comme modèle de repli.
4. **Fusion** : le véhicule est posé sur l'arrière-plan choisi (recadré *cover* aux dimensions d'origine), aligné en bas et centré, ou exporté sur fond transparent (PNG).

> 🔁 **Repli automatique** : si le modèle ONNX de plaques est absent ou corrompu, une cascade de Haar OpenCV prend le relais (précision moindre, plaques de type européen). L'état du moteur est visible en haut de l'interface et sur `/api/health`.

---

## ⚙️ Configuration

Variables d'environnement (toutes optionnelles) :

| Variable | Défaut | Description |
|---|---|---|
| `APP_PASSWORD` | *(vide)* | Mot de passe de l'outil. **Vide = accès non protégé** (avertissement) |
| `APP_SECRET` | *(généré)* | Clé de signature des sessions. Sinon générée et conservée dans `DATA_DIR/.secret` |
| `SESSION_HOURS` | `12` | Durée d'une session de connexion |
| `TRUSTED_PROXY` | `0` | `1` si l'app est derrière un reverse proxy de confiance (lit `X-Forwarded-For` / `-Proto`) |
| `MAX_WORKERS` | `2` (`1` dans le compose) | Workers CPU parallèles (rembg / détection) |
| `MAX_FILE_MB` | `25` | Taille maximale par image |
| `MAX_TOTAL_MB` | `400` | Taille cumulée maximale d'un lot |
| `MAX_PIXELS` | `120000000` | Dimensions maximales d'une image (protection mémoire) |
| `MAX_FILES` | `60` | Nombre maximal d'images par lot |
| `JOB_TTL_MINUTES` | `1440` | Durée de conservation des résultats avant purge (24 h) |
| `REMBG_MODEL` / `REMBG_MAX_MODEL` / `REMBG_FAST_MODEL` | `birefnet-general-lite` / `birefnet-general-lite` / `isnet-general-use` | Modèles de détourage proposés |
| `PLATE_CONF_THRESHOLD` | `0.30` | Seuil de confiance de la détection de plaques |
| `PLATE_WEAK_THRESHOLD` | `0.2` | Détections entre ce seuil et le précédent : retenues seulement si un contour de plaque net est trouvé (`0` = désactivé) |
| `PLATE_MODEL_PATH` | `/app/models/plate_yolov8.onnx` | Chemin du détecteur YOLO ONNX |
| `DATA_DIR` | `/data` | Données persistantes (fonds + jobs) |
| `MODELS_DIR` | `/app/models` | Emplacement des modèles |

---

## 🌐 API

La documentation Swagger interactive est disponible sur **`/api/docs`** (après connexion).

**Authentification** : quand `APP_PASSWORD` est défini, tout `/api/*` exige soit le cookie de session (UI), soit l'en-tête `Authorization: Bearer <mot de passe>` (scripts). Seuls `/api/auth/*` et `/api/health` (réponse minimale) sont publics. Après 5 échecs en 5 minutes, l'IP reçoit un `429`.

| Méthode | Route | Description |
|---|---|---|
| POST | `/api/auth/login` | Connexion (`{"password": "..."}`), pose le cookie de session |
| POST | `/api/auth/logout` | Déconnexion |
| GET | `/api/auth/status` | `auth_required` / `authenticated` |
| GET | `/api/health` | État du service + moteur de plaques actif |
| GET | `/api/backgrounds` | Liste des arrière-plans enregistrés |
| POST | `/api/backgrounds` | Enregistre un nouveau fond (multipart `file`, `name`) |
| DELETE | `/api/backgrounds/{id}` | Supprime un fond |
| POST | `/api/jobs` | Traite un lot d'images (multipart) |
| GET | `/api/jobs/{id}` | Progression + résultats du job |
| GET | `/api/results/{id}/{index}?download=1` | Télécharge un résultat |
| POST | `/api/retouch/{id}/{index}` | Retouche manuelle : `{"polygon": [[x,y],...], "strength": 1-5, "style": "blur\|white"}` (3 à 64 points, pixels de l'image réelle) |

Exemple : traiter deux images avec remplacement de fond :

```bash
AUTH="Authorization: Bearer $APP_PASSWORD"
JOB=$(curl -s -X POST http://localhost:8000/api/jobs -H "$AUTH" \
  -F "files=@photo1.jpg" \
  -F "files=@photo2.jpg" \
  -F "bg_mode=replace" \
  -F "background_id=abc123def456" \
  -F "blur_enabled=true" \
  -F "blur_strength=3" | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")

curl -s -H "$AUTH" http://localhost:8000/api/jobs/$JOB
```

---

## 📦 Modèles embarqués

| Modèle | Taille | Rôle | Source |
|---|---|---|---|
| `birefnet-general-lite.onnx` | ~224 Mo | Détourage qualité maximale (BiRefNet, par défaut) | Releases officielles [rembg](https://github.com/danielgatis/rembg) |
| `isnet-general-use.onnx` | ~178 Mo | Détourage rapide (ISNet) | Releases officielles [rembg](https://github.com/danielgatis/rembg) |
| `u2net.onnx` | ~176 Mo | Détourage de repli (U²-Net) | Releases officielles [rembg](https://github.com/danielgatis/rembg) |
| `plate_yolov8.onnx` | ~38 Mo | Détection de plaques (YOLO11s) | [morsetechlab/yolov11-license-plate-detection](https://huggingface.co/morsetechlab/yolov11-license-plate-detection) (Hugging Face, ~72k téléchargements) : repli auto : [ml-debi/yolov8-license-plate-detection](https://huggingface.co/ml-debi/yolov8-license-plate-detection) |

Les modèles sont téléchargés au **build** (script `scripts/download_models.py`, idempotent) puis embarqués dans l'image. Chaque fichier est **vérifié par SHA-256** : si une source distante change légitimement, mettez à jour l'empreinte dans le script.

**Utiliser votre propre détecteur** : tout YOLOv8/YOLO11 de détection exporté ONNX (entrée 640×640, une classe) fonctionne. Montez le fichier dans le conteneur et pointez `PLATE_MODEL_PATH` dessus.

---

## 🧪 Développement

```
garage-image-tool/
├── app/
│   ├── main.py            # API FastAPI
│   ├── auth.py            # Authentification (mot de passe, session, limiteur)
│   ├── processing.py      # Pipeline : détourage + fond + flou
│   ├── plate_detector.py  # YOLO ONNX + repli Haar + ajustement du quadrilatère
│   ├── store.py           # Persistance fonds / jobs
│   ├── config.py          # Configuration
│   └── static/            # Frontend (HTML/CSS/JS vanilla)
├── scripts/
│   └── download_models.py # Téléchargement + vérification SHA-256 des modèles (build)
├── tests/                 # Tests pytest (auth, pipeline, API, store, modèles)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

Lancer les tests (aucun modèle nécessaire : rembg et YOLO sont simulés) :

```bash
pip install -r requirements-dev.txt
pytest
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

- Plaques **très petits ou très angulées** (photo lointaine, vue arrière en contre-plongée extrême) : la détection peut échouer. Vérifiez le badge « plaques floutées » par image dans l'interface.
- Le détourage peut occasionnellement accrocher des reflets complexes (vitres très teintées), rare sur des photos de véhicule standard.
- En mode repli Haar (sans modèle ONNX), la couverture pays est réduite.
- Téléchargement multiple : Chrome/Firefox demandent une autorisation unique « télécharger plusieurs fichiers ». Safari/iOS peuvent limiter le nombre de téléchargements consécutifs.

## 🔐 RGPD / Confidentialité

- Les images sont traitées **intégralement sur votre serveur** : aucune donnée n'est envoyée à un service tiers, aucun appel réseau pendant le traitement.
- Les photos **originales (plaques non floutées) sont supprimées du disque dès la fin du traitement** ; seuls les résultats restent, puis sont **purgés automatiquement** (24 h par défaut, `JOB_TTL_MINUTES`).
- L'accès est protégé par mot de passe (`APP_PASSWORD`) ; exposez l'outil derrière HTTPS (reverse proxy) et définissez `TRUSTED_PROXY=1` si vous en utilisez un.
- Le floutage des plaques vise la conformité RGPD pour la publication de photos de véhicules (données personnelles = plaques + visages éventuels ; pensez aussi aux visages si présents).

## 📄 Licence

MIT : voir [LICENSE](LICENSE).
