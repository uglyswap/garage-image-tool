#!/bin/sh
# Remplace le conteneur garage-image-tool par une nouvelle image, avec retour arriere automatique.
# Usage : scripts/deploy.sh [tag]            (defaut : latest)
#   ex. : docker build -t garage-image-tool:latest . && sh scripts/deploy.sh
#
# Variables (toutes optionnelles) :
#   ENV_FILE      fichier d'environnement (APP_PASSWORD, TRUSTED_PROXY...)   defaut : ./.env
#   BIND_ADDR     adresse d'ecoute ; 127.0.0.1 ou l'adresse du pont Docker derriere un proxy HTTPS
#   PORT          port publie                                               defaut : 8000
#   MEMORY_LIMIT  plafond memoire du conteneur                              defaut : 8g
#   ROLLBACK_TAG  image relancee si la nouvelle ne devient pas saine        defaut : aucune
# Le volume garage-data (fonds, resultats, secret de session) est toujours conserve.
set -u
TAG="${1:-latest}"
NAME=garage-image-tool
ENV_FILE="${ENV_FILE:-./.env}"
BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
PORT="${PORT:-8000}"
MEMORY_LIMIT="${MEMORY_LIMIT:-8g}"
ROLLBACK_TAG="${ROLLBACK_TAG:-}"

[ -f "$ENV_FILE" ] || { echo "ECHEC : $ENV_FILE introuvable (APP_PASSWORD y est attendu)"; exit 1; }

start() {
  docker stop "$NAME" >/dev/null 2>&1
  docker rm "$NAME" >/dev/null 2>&1
  docker run -d --name "$NAME" --restart unless-stopped \
    --memory "$MEMORY_LIMIT" --memory-swap "$MEMORY_LIMIT" \
    -p "$BIND_ADDR:$PORT:8000" --env-file "$ENV_FILE" -v garage-data:/data "$NAME:$1" >/dev/null
}

healthy() {
  i=0
  while [ $i -lt 40 ]; do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null)" = "healthy" ] && return 0
    i=$((i + 1))
    sleep 5
  done
  return 1
}

start "$TAG" || { echo "ECHEC : demarrage de $NAME:$TAG impossible"; exit 1; }
if healthy; then
  echo "OK : $TAG en service"
  exit 0
fi
echo "ECHEC : $TAG ne repond pas"
docker logs --tail 30 "$NAME"
if [ -n "$ROLLBACK_TAG" ]; then
  echo "Retour arriere vers $ROLLBACK_TAG"
  start "$ROLLBACK_TAG" && healthy && echo "OK : $ROLLBACK_TAG de nouveau en service"
fi
exit 1
