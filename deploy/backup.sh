#!/usr/bin/env bash
# Nightly (and pre-update) backup: schema+data dump plus blob volume archive.
# Restore: pg_restore -U jbrain -d jbrain --clean <dump>; untar blobs into the
# volume. The restore path must be exercised before the system holds real data.
set -euo pipefail

cd /opt/jbrain
STAMP="$(date +%Y%m%d-%H%M%S)"
KEEP_DAYS=14

mkdir -p backups

docker compose exec -T db pg_dump -U jbrain -Fc jbrain > "backups/jbrain-$STAMP.dump"

# Blob volume lands in Phase 1; archive it once it exists.
if docker volume inspect jbrain_blobs >/dev/null 2>&1; then
  docker run --rm -v jbrain_blobs:/blobs:ro -v /opt/jbrain/backups:/out alpine \
    tar czf "/out/blobs-$STAMP.tar.gz" -C /blobs .
fi

find backups -name '*.dump' -mtime +"$KEEP_DAYS" -delete
find backups -name 'blobs-*.tar.gz' -mtime +"$KEEP_DAYS" -delete

echo "backup complete: $STAMP"
