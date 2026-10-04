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

if ! [[ "$STAMP" =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
  echo "not a backup stamp: $STAMP (expected e.g. 20260610-031500)" >&2
  exit 1
fi

DUMP="backups/jbrain-$STAMP.dump"
if [ ! -f "$DUMP" ]; then
  echo "no such dump: $DUMP" >&2
  exit 1
fi

# --- pick the blob archive (tested by supervisor/tests/test_backup_retention.py) ---
# backup.sh skips the blob tarball when the volume is unchanged, so a dump's blobs are
# not always its own stamp's. It records the pairing in jbrain-<stamp>.blobs, and that
# is the answer whenever it exists. A dump from before the sidecar always wrote its own
# same-stamp tarball, so only that exact file counts: ordering stamps written in
# different zones could pick an OLDER tarball and drop every attachment added since.
BLOBS=""
SIDECAR="backups/jbrain-$STAMP.blobs"
if [ -f "$SIDECAR" ]; then
  paired="$(cat "$SIDECAR")"
  if [[ "$paired" =~ ^[0-9]{8}-[0-9]{6}$ ]]; then
    if [ -f "backups/blobs-$paired.tar.gz" ]; then
      BLOBS="backups/blobs-$paired.tar.gz"
    else
      echo "warning: this dump pairs with blobs-$paired.tar.gz, which is gone" >&2
    fi
  fi
elif [ -f "backups/blobs-$STAMP.tar.gz" ]; then
  BLOBS="backups/blobs-$STAMP.tar.gz"
fi
# --- end pick ---

# Read the whole archive BEFORE anything changes. A tarball cut short by a full disk
# (the old backup.sh wrote it straight to its final name) would otherwise wipe the
# volume and then extract half of it. A damaged archive stops the restore here, with
# the database and the attachments both untouched.
if [ -n "$BLOBS" ]; then
  if ! docker run --rm -v /opt/jbrain2/backups:/in:ro alpine \
    tar tzf "/in/${BLOBS#backups/}" >/dev/null; then
    echo "error: ${BLOBS#backups/} is damaged or truncated — restore aborted, nothing changed" >&2
    exit 1
  fi
fi

# Writers must be off the database before objects get dropped; the db
# container itself stays up to run the restore.
docker compose stop api worker

# --clean --if-exists drops and recreates everything in the dump — tables,
# extensions, RLS policies, and grants — so the app role's least-privilege
# setup survives the round trip. Runs as the superuser, same as migrations.
docker compose exec -T db pg_restore -U jbrain -d jbrain \
  --clean --if-exists --exit-on-error < "$DUMP"

# Blob store: replace the volume contents with the paired archive. With none (the
# sidecar says "none", or names a tarball that is gone) the volume is left as-is:
# wiping it with no archive to extract would be the one unrecoverable step here.
if [ -n "$BLOBS" ]; then
  echo "blobs from ${BLOBS#backups/}"
  docker run --rm -v jbrain_blobs:/blobs -v /opt/jbrain2/backups:/in:ro alpine \
    sh -c "tar tzf '/in/${BLOBS#backups/}' >/dev/null && find /blobs -mindepth 1 -delete && tar xzf '/in/${BLOBS#backups/}' -C /blobs"
else
  echo "warning: no blob archive for $STAMP — attachment bytes left as-is" >&2
fi

docker compose up -d

echo "restore complete: $STAMP"
