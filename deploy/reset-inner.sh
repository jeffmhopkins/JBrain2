#!/bin/sh
# Containerized reset, launched by the supervisor as a detached one-shot
# (docker:cli) like import-inner.sh. A testing convenience: erases all
# content data and blobs while the stack — and the owner's session — stays
# up. TRUNCATE needs table ownership and RLS does not bind it, so the api's
# least-privilege role deliberately cannot do this; that is why erasing data
# is a supervisor one-shot running superuser psql, never an api-side query.
set -eu

echo "[reset] starting"

echo "[reset] safety backup"
./backup.sh

# The worker may hold a claimed job whose rows are about to vanish; stop it
# across the truncate so it comes back to a clean queue.
echo "[reset] stopping worker"
docker compose stop worker

# Content tables only, one statement so it is all-or-nothing. Kept on
# purpose: app.subjects / app.principals / app.device_sessions (the owner
# stays logged in), app.domains (seed data), app.llm_usage (spend telemetry
# survives resets), and alembic_version (the schema is untouched).
echo "[reset] truncating content tables"
docker compose exec -T db psql -U jbrain -d jbrain -v ON_ERROR_STOP=1 -c \
  "TRUNCATE app.notes, app.attachments, app.chunks, app.jobs, app.facts,
            app.entities, app.entity_mentions, app.entity_aliases,
            app.entity_distinctions, app.temporal_tokens, app.review_items,
            app.note_analysis CASCADE"

echo "[reset] clearing blobs"
docker run --rm -v jbrain_blobs:/blobs alpine find /blobs -mindepth 1 -delete

echo "[reset] starting worker"
docker compose start worker

echo "[reset] complete"
