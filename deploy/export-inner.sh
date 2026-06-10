#!/bin/sh
# Containerized export, launched by the supervisor as a detached one-shot
# (docker:cli) like update-inner.sh. Bundles a pg_dump, the blob volume,
# and a small manifest into one archive the PWA downloads via the api's
# read-only backups mount. Only the newest export is kept — exports are
# downloads, not retention (nightly backups handle that).
set -eu

STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="backups/.export-$STAMP"
OUT="export-$STAMP.jbrain.tar"

echo "[export] starting"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

echo "[export] dumping database"
docker compose exec -T db pg_dump -U jbrain -Fc jbrain > "$WORK/db.dump"

echo "[export] archiving blobs"
docker run --rm -v jbrain_blobs:/blobs:ro -v "$(pwd)/$WORK:/out" alpine \
  tar czf /out/blobs.tar.gz -C /blobs .

SCHEMA="$(docker compose exec -T db psql -U jbrain -d jbrain -tA \
  -c 'SELECT version_num FROM alembic_version')"
NOTES="$(docker compose exec -T db psql -U jbrain -d jbrain -tA \
  -c 'SELECT count(*) FROM app.notes WHERE deleted_at IS NULL')"
cat > "$WORK/manifest.json" <<EOF
{"stamp": "$STAMP", "schema": "$SCHEMA", "notes": $NOTES, "format": 1}
EOF

echo "[export] bundling"
tar cf "backups/$OUT" -C "$WORK" manifest.json db.dump blobs.tar.gz
rm -f $(ls backups/export-*.jbrain.tar 2>/dev/null | grep -v "$OUT") 2>/dev/null || true

echo "[export] file: $OUT"
echo "[export] complete"
