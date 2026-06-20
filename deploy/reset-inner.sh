#!/bin/sh
# Containerized reset, launched by the supervisor as a detached one-shot
# (docker:cli) like import-inner.sh. A testing convenience: returns the
# database to its just-migrated factory state and clears all blobs, carrying
# ONLY the owner key (and its live sessions) across — so the owner is neither
# locked out nor logged out, while everything else (all notes/content, every
# derived row, all other principals/keys, and even seed-table edits) is gone.
#
# Why a full schema rebuild instead of a TRUNCATE allow-list: the app schema
# holds dozens of tables and grows every phase, so a hand-maintained "content
# tables" list silently goes stale and leaves new tables' data behind. Dropping
# and re-migrating resets *everything* by construction and restores seed data
# (the domains firewall, the workflow triggers/schedules/actions) to defaults
# in one step. DROP SCHEMA + alembic both need the superuser role, which is why
# this is a supervisor one-shot and never an api-side query — the api's
# least-privilege role can do neither.
set -eu

echo "[reset] starting"

echo "[reset] safety backup"
./backup.sh

# Stash the live owner key + its sessions in the public schema, which survives
# the app-schema drop, so the same owner key still logs in and existing owner
# cookies stay valid across the rebuild. Superuser psql bypasses RLS, so this
# sees the rows the api role never could.
echo "[reset] preserving owner key"
docker compose exec -T db psql -U jbrain -d jbrain -v ON_ERROR_STOP=1 -c \
  "DROP TABLE IF EXISTS public._reset_keep_principals, public._reset_keep_sessions;
   CREATE TABLE public._reset_keep_principals AS
     SELECT * FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL;
   CREATE TABLE public._reset_keep_sessions AS
     SELECT s.* FROM app.device_sessions s
       JOIN public._reset_keep_principals p ON p.id = s.principal_id
      WHERE s.revoked_at IS NULL"

# Stop the writers so the schema can be dropped out from under them and the api
# never serves a query against half-rebuilt tables; the import one-shot stops the
# same pair for the same reason. The PWA's reset view tolerates the api gap (it
# polls /reset/status through the api and flags 'unreachable' meanwhile).
echo "[reset] stopping writers"
docker compose stop api worker

echo "[reset] dropping schema"
docker compose exec -T db psql -U jbrain -d jbrain -v ON_ERROR_STOP=1 -c \
  "DROP SCHEMA IF EXISTS app CASCADE; DROP TABLE IF EXISTS public.alembic_version"

# Rebuild via the same migration runner a deploy uses, so the factory state and
# its seed data are exactly what `alembic upgrade head` produces.
echo "[reset] rebuilding schema"
docker compose run --rm migrate

echo "[reset] restoring owner key"
docker compose exec -T db psql -U jbrain -d jbrain -v ON_ERROR_STOP=1 -c \
  "INSERT INTO app.principals
     (id, kind, subject_id, key_hash, label, created_at, revoked_at)
     SELECT id, kind, subject_id, key_hash, label, created_at, revoked_at
       FROM public._reset_keep_principals;
   INSERT INTO app.device_sessions
     (id, principal_id, token_hash, label, created_at, last_seen_at, revoked_at)
     SELECT id, principal_id, token_hash, label, created_at, last_seen_at, revoked_at
       FROM public._reset_keep_sessions;
   DROP TABLE public._reset_keep_principals, public._reset_keep_sessions"

echo "[reset] clearing blobs"
docker run --rm -v jbrain_blobs:/blobs alpine find /blobs -mindepth 1 -delete

echo "[reset] restarting stack"
docker compose up -d

echo "[reset] complete"
