#!/bin/sh
# Nightly (and pre-update) backup: schema+data dump plus blob volume archive.
# Restore with restore.sh (jbrain restore <stamp>) — drilled end-to-end; keep
# both sides in step when adding volumes or moving data outside Postgres.
#
# POSIX sh (not bash): the import/reset one-shots call this from inside the
# bash-less docker:cli (Alpine) container, so a `#!/usr/bin/env bash` shebang
# fails there with "env: can't execute 'bash'". No pipes here, so dropping
# `pipefail` loses nothing.
set -eu

cd /opt/jbrain2
STAMP="$(date +%Y%m%d-%H%M%S)"
KEEP_DAYS=14

mkdir -p backups

docker compose exec -T db pg_dump -U jbrain -Fc jbrain > "backups/jbrain-$STAMP.dump"

# Blob volume lands in Phase 1; archive it once it exists.
if docker volume inspect jbrain_blobs >/dev/null 2>&1; then
  docker run --rm -v jbrain_blobs:/blobs:ro -v /opt/jbrain2/backups:/out alpine \
    tar czf "/out/blobs-$STAMP.tar.gz" -C /blobs .
fi

find backups -name '*.dump' -mtime +"$KEEP_DAYS" -delete
find backups -name 'blobs-*.tar.gz' -mtime +"$KEEP_DAYS" -delete

echo "backup complete: $STAMP"
