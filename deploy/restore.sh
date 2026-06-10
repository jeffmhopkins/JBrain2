#!/usr/bin/env bash
# Restore a backup taken by backup.sh: database dump + blob archive.
# Drilled end-to-end (backup → wipe → restore → verify) before the system
# held real data; keep it that way after schema changes that add volumes
# or move data outside Postgres.
#
# Usage: ./restore.sh <stamp>        e.g. ./restore.sh 20260610-031500
#        ./restore.sh                lists available backups
set -euo pipefail

cd /opt/jbrain2

STAMP="${1:-}"
if [ -z "$STAMP" ]; then
  echo "usage: restore.sh <stamp>"
  echo "available backups:"
  ls -1 backups/jbrain-*.dump 2>/dev/null | sed 's|backups/jbrain-||; s|\.dump||' || echo "  (none)"
  exit 1
fi

DUMP="backups/jbrain-$STAMP.dump"
BLOBS="backups/blobs-$STAMP.tar.gz"
if [ ! -f "$DUMP" ]; then
  echo "no such dump: $DUMP" >&2
  exit 1
fi

# Writers must be off the database before objects get dropped; the db
# container itself stays up to run the restore.
docker compose stop api worker

# --clean --if-exists drops and recreates everything in the dump — tables,
# extensions, RLS policies, and grants — so the app role's least-privilege
# setup survives the round trip. Runs as the superuser, same as migrations.
docker compose exec -T db pg_restore -U jbrain -d jbrain \
  --clean --if-exists --exit-on-error < "$DUMP"

# Blob store: replace the volume contents with the archived tree. The dump
# and archive share a stamp, so notes and their attachment bytes stay in
# step.
if [ -f "$BLOBS" ]; then
  docker run --rm -v jbrain_blobs:/blobs -v /opt/jbrain2/backups:/in:ro alpine \
    sh -c "find /blobs -mindepth 1 -delete && tar xzf '/in/blobs-$STAMP.tar.gz' -C /blobs"
else
  echo "warning: no blob archive for $STAMP — attachment bytes left as-is" >&2
fi

docker compose up -d

echo "restore complete: $STAMP"
