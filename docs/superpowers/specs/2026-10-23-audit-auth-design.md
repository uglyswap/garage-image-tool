# Audit, corrections et authentification : spécification

Date : 2026-10-23
Version cible : 1.1.0
Dépôt : uglyswap/garage-image-tool (livraison par commits sur `main`)

## 1. Objectif et contraintes

Corriger les défauts relevés à l'audit complet du code, durcir la confidentialité (RGPD) et ajouter une authentification par mot de passe unique partagé.

Contraintes validées :
- Authentification : **mot de passe unique partagé** (`APP_PASSWORD`), cookie de session signé pour l'UI, `Authorization: Bearer` pour l'API, aucune nouvelle dépendance.
- Sans `APP_PASSWORD` : **démarrage en mode ouvert** avec avertissement (logs + bandeau UI).
- **Aucune suppression de code ou de fonctionnalité** sans accord. Le téléchargement de `u2net.onnx` et son rôle de repli sont **conservés**. La seule suppression runtime autorisée est celle des originaux `in_N` après traitement.
- Livraison : commits par lot logique, push direct sur `main`.

Hors périmètre : comptes multiples, annulation (undo) des retouches, ZIP, CI, refonte du pipeline de détourage.

## 2. Authentification

### 2.1 Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `APP_PASSWORD` | vide | Active l'authentification quand défini |
| `APP_SECRET` | généré | Clé de signature des cookies. Sinon aléatoire, persistée dans `/data/.secret` (droits 600) |
| `SESSION_HOURS` | `12` | Durée de vie d'une session |
| `TRUSTED_PROXY` | `0` | Si `1`, l'IP client est lue dans `X-Forwarded-For` |

### 2.2 Composant `app/auth.py`

Responsabilité unique : décider si une requête est authentifiée et fournir les routes de session.

- `AuthConfig` : lit les variables, charge ou crée le secret.
- `make_token(expiry) -> str` : `"<expiry>.<hmac_sha256_hex>"`. `verify_token(token) -> bool` : recalcul + `hmac.compare_digest` + contrôle d'expiration.
- `is_authenticated(request) -> bool` : cookie `gi_session` valide, ou en-tête `Authorization: Bearer <mot de passe>` (comparaison `compare_digest`).
- `LoginLimiter` : 5 échecs par fenêtre de 5 minutes par IP, puis 429 avec `Retry-After`. Stockage en mémoire, nettoyage opportuniste. Un échec ajoute un délai de 0,5 s.
- Middleware HTTP : pour tout chemin commençant par `/api/`, hors liste blanche, renvoie 401 JSON `{"detail": "Authentification requise"}` si non authentifié.
- Liste blanche : `/api/auth/login`, `/api/auth/logout`, `/api/auth/status`, `/api/health`. Les fichiers statiques (`/`, `/app.js`, `/style.css`) restent publics et ne contiennent aucun secret.

**Pourquoi un middleware avec liste blanche :** tout futur endpoint `/api/*` est protégé par défaut, il est impossible de l'exposer par oubli.

### 2.3 Routes

- `POST /api/auth/login` corps JSON `{"password": "..."}` : 204 + `Set-Cookie` si correct, 401 sinon, 429 si limité.
- `POST /api/auth/logout` : supprime le cookie.
- `GET /api/auth/status` : `{"auth_required": bool, "authenticated": bool}`.
- `GET /api/health` : authentifié ou mode ouvert, réponse complète actuelle + `auth_required`. Non authentifié : `{"status", "version", "auth_required"}` uniquement (le HEALTHCHECK Docker continue de fonctionner).

### 2.4 Cookie

`gi_session`, `HttpOnly`, `SameSite=Strict`, `Path=/`, `Secure` si le schéma est HTTPS (ou `X-Forwarded-Proto: https` quand `TRUSTED_PROXY=1`), `Max-Age = SESSION_HOURS * 3600`.

**Pourquoi `SameSite=Strict` :** les routes qui modifient l'état sont des POST/DELETE cookie-authentifiés. Strict neutralise le CSRF sans jeton supplémentaire. Les appels Bearer ne sont pas vulnérables au CSRF.

### 2.5 Mode ouvert

Sans `APP_PASSWORD` : le middleware laisse tout passer, un `logger.warning` est émis au démarrage, `/api/auth/status` renvoie `auth_required: false` et l'UI affiche un bandeau « Accès non protégé : définissez APP_PASSWORD ».

### 2.6 Frontend

- `index.html` : écran de connexion (`#login`), bouton « Se déconnecter », bandeau de mode ouvert.
- `app.js` : `init()` interroge `/api/auth/status` avant tout autre appel. Une fonction `api(url, opts)` centralise `fetch`, renvoie à l'écran de connexion sur 401 et alimente les messages d'erreur. Les `fetch` existants passent par elle.

## 3. Corrections de bugs

| # | Fichier | Correction |
|---|---|---|
| 1 | `config.py`, `main.py:189` | Ajouter `REMBG_FAST_MODEL = "isnet-general-use"`. Modèles autorisés : `{REMBG_MODEL, REMBG_MAX_MODEL, REMBG_FAST_MODEL}`. |
| 2 | `processing.py:285` | `retouch_bytes` lit en `IMREAD_UNCHANGED`. Si 4 canaux, la retouche s'applique aux 3 canaux BGR et l'alpha est réassemblé avant encodage. |
| 3 | `processing.py:87` | Le décalage `dx`/`dy` crée de nouvelles `PlateBox` en **préservant `pts`** (points décalés). |
| 4 | `processing.py:87,103` | Après décalage, bornage dans l'image. Une plaque entièrement hors image est ignorée, une plaque partielle est recadrée. Plus d'indices négatifs dans le slicing. |
| 5 | `main.py:299` | En-tête `filename*=UTF-8''<nom encodé>` (deux apostrophes). |
| 6 | `app.js:172` | `poll()` s'arrête sur 404 avec message « Résultats expirés ». Tolère 5 erreurs réseau consécutives, puis affiche l'erreur. Un 401 renvoie à la connexion. |
| 7 | `store.py:192` | Au rechargement, les items non terminés et non repris passent en `error` ; le job est clos (`done = total`, `status = "done"`). |
| 8 | `main.py:342` | Verrou dédié par job pour la retouche + écriture atomique (`tmp` puis `os.replace`). |

## 4. RGPD et validation des entrées

- **Suppression des originaux** : `_process_one` supprime `in_N` dans son bloc `finally` une fois l'item `done` ou `error`. Les `in_N` d'items `pending` sont conservés (nécessaires à la reprise après redémarrage).
- **Uploads vers le disque** : `_read_limited` est remplacée par une écriture en flux vers le fichier cible, avec plafond par fichier (`MAX_FILE_MB`) et plafond cumulé (`MAX_TOTAL_MB`, défaut 400). Plus de chargement de tous les fichiers en RAM.
- **Octets réels** : après écriture, `Image.open(...).verify()` contrôle que le fichier est bien un JPEG/PNG/WebP. Rejet 415 sinon. Même contrôle pour les fonds (`add_background`).
- **Bornes** : `jpeg_quality` 50 à 100, `blur_strength` 1 à 5 (`Form(ge=, le=)`).
- **Retouche** : corps validé par un modèle Pydantic (`polygon`: 3 à 64 points, `strength`: 1 à 5, `style`: `blur|white`). Coordonnées bornées à la taille de l'image.
- **Intégrité des modèles** : `scripts/download_models.py` épingle un SHA-256 par modèle et refuse un fichier dont l'empreinte diffère. Les empreintes sont calculées une fois depuis les téléchargements actuels.

## 5. Docker, store, front

- **Dockerfile** : `COPY --chown=app:app` pour `app/` et `scripts/`, `chown` limité à `/data`, ce qui évite de dupliquer dans une nouvelle couche les plus de 700 Mo de modèles (lisibles par `app` sans changement de propriétaire). Le téléchargement de `u2net.onnx` est conservé.
- **store.py** : verrou sur `BackgroundStore.list/get`, `try/except` autour du corps de `cleanup_loop` (le thread ne meurt plus).
- **main.py** : suppression du `time.sleep(4)` de `_resume_interrupted_jobs` ; la reprise est lancée dans la tâche de démarrage.
- **Front** : libellé de résultat selon le style (« plaque floutée » / « plaque masquée ») et l'option désactivée ; `role="button"` + `tabindex` + Entrée/Espace sur les fonds ; Échap et recalcul de l'échelle au redimensionnement dans la retouche.

## 6. Documentation et configuration

- `README.md` : modèles réels (BiRefNet/ISNet, u2net en repli), TTL de 24 h, route `/api/retouch`, section « Authentification », nouvelles variables.
- `docker-compose.yml` : `APP_PASSWORD: ${APP_PASSWORD:-}`.
- `config.py` : `VERSION = "1.1.0"`.
- `.gitignore` : ajouter `.secret`.

## 7. Tests (pytest, `tests/` miroir de `app/`)

- `tests/test_auth.py` : mot de passe correct/incorrect, cookie valide/expiré/falsifié, Bearer, limitation (429), liste blanche, mode ouvert.
- `tests/test_processing.py` : retouche conserve l'alpha d'un PNG ; décalage conserve `pts` ; plaque hors image ignorée.
- `tests/test_main.py` : modèle ISNet accepté, modèle inconnu refusé (400) ; en-tête `filename*` correct ; bornes de formulaire ; fichier non-image refusé (415) ; `in_N` supprimé après traitement.
- `tests/test_store.py` : rechargement d'un job bloqué le clôt.
- Ajout de `requirements-dev.txt` (`pytest`, `httpx`).

Les tests évitent de charger les vrais modèles (rembg et YOLO sont simulés via `monkeypatch`).

## 8. Plan de livraison (commits sur `main`)

1. Authentification (`auth.py`, middleware, front, config, compose, tests).
2. Corrections de bugs #1 à #8 (+ tests).
3. RGPD et validation des entrées (+ tests).
4. Dockerfile, store, hashes des modèles.
5. Front : accessibilité et polling.
6. README, version, `.gitignore`.

## 9. Critères de réussite

- `pytest` passe intégralement.
- Avec `APP_PASSWORD` défini : tout `/api/*` hors liste blanche renvoie 401 sans cookie ni Bearer ; l'UI affiche la connexion.
- Sans `APP_PASSWORD` : comportement actuel conservé, avec avertissement visible.
- L'option « Rapide : ISNet » fonctionne de bout en bout.
- Aucun `in_N` ne subsiste après un job terminé.
- Aucune suppression de code hors celle validée.

## 10. Risques

- Les déploiements existants sans `APP_PASSWORD` restent ouverts : c'est le comportement choisi, signalé par le bandeau et les logs.
- Les hashes de modèles doivent être mis à jour si un fichier distant change : l'échec du build est alors volontaire.
- Le build Docker complet n'est pas exécutable dans cet environnement sans télécharger plus de 700 Mo : sa validation reposera sur une relecture et un build manuel côté utilisateur.
