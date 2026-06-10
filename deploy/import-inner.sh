#!/bin/sh
# Containerized import, launched by the supervisor as a detached one-shot
# (docker:cli) so it survives stopping the very stack it restores. Takes an
# archive name under backups/ (uploaded through the api), takes a safety
# backup, then runs restore.sh's exact commands against the extracted
# dump + blobs. Destructive by design — the PWA arms the confirm.
set -eu

NAME="${1:?usage: import-inner.sh <archive-name>}"
ARCHIVE="backups/$NAME"
[ -f "$ARCHIVE" ] || { echo "[import] no such archive: $NAME" >&2; exit 1; }

WORK="backups/.import-$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

echo "[import] starting: $NAME"
tar xf "$ARCHIVE" -C "$WORK"
[ -f "$WORK/db.dump" ] || { echo "[import] archive has no db.dump" >&2; exit 1; }

echo "[import] safety backup of current data"
./backup.sh

echo "[import] stopping writers"
docker compose stop api worker

echo "[import] restoring database"
docker compose exec -T db pg_restore -U jbrain -d jbrain \
  --clean --if-exists --exit-on-error < "$WORK/db.dump"

if [ -f "$WORK/blobs.tar.gz" ]; then
  echo "[import] restoring blobs"
  docker run --rm -v jbrain_blobs:/blobs -v "$(pwd)/$WORK:/in:ro" alpine \
    sh -c "find /blobs -mindepth 1 -delete && tar xzf /in/blobs.tar.gz -C /blobs"
fi

echo "[import] restarting stack"
docker compose up -d

rm -f "$ARCHIVE"
echo "[import] complete"
