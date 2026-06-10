#!/usr/bin/env bash
# Backup/restore drill — proves deploy/backup.sh and deploy/restore.sh stay
# inverse operations. Stands up a production-image Postgres with the real
# schema (alembic) plus seeded data and a blob volume, backs both up with
# backup.sh's exact commands, destroys everything, restores with
# restore.sh's exact commands, then verifies data, RLS scoping, and blob
# bytes all survived. Re-run after schema changes, new volumes, or anything
# that moves data outside Postgres. Needs Docker and uv; ~2 min.
set -euo pipefail

IMG=timescale/timescaledb-ha:pg17
PGPW=drillsuper
APPPW=drillapp
DRILL="$(mktemp -d /tmp/jbrain-restore-drill.XXXXXX)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
export PGPASSWORD=$PGPW

step() { printf '\n=== %s ===\n' "$*"; }

cleanup() {
  docker rm -f drill-db >/dev/null 2>&1 || true
  docker volume rm -f drill_blobs >/dev/null 2>&1 || true
}
trap cleanup EXIT

psql_super() { docker exec -i drill-db psql -v ON_ERROR_STOP=1 -U jbrain -d jbrain -tA "$@"; }

# psql prints BEGIN/COMMIT tags too; the count is the last purely-numeric line.
app_count() { psql -h localhost -U jbrain_app -d jbrain -tA | grep -E '^[0-9]+$' | tail -1; }

start_db() {
  docker volume create drill_blobs >/dev/null
  docker run -d --name drill-db --network host \
    -e POSTGRES_DB=jbrain -e POSTGRES_USER=jbrain -e POSTGRES_PASSWORD=$PGPW \
    -e APP_DB_PASSWORD=$APPPW \
    -v "$REPO/deploy/db-init:/docker-entrypoint-initdb.d:ro" \
    $IMG >/dev/null
  until docker exec drill-db pg_isready -U jbrain -d jbrain >/dev/null 2>&1; do sleep 2; done
  sleep 3   # this image flaps ready once mid-init; give the init scripts a beat
}

fingerprint() {
  psql_super <<'SQL'
SELECT 'notes', count(*), coalesce(md5(string_agg(body, '|' ORDER BY id)), '-') FROM app.notes;
SELECT 'attachments', count(*), coalesce(md5(string_agg(sha256, '|' ORDER BY id)), '-') FROM app.attachments;
SELECT 'chunks', count(*), coalesce(md5(string_agg(embedding::text || text, '|' ORDER BY note_id)), '-') FROM app.chunks;
SELECT 'policies', count(*), coalesce(md5(string_agg(policyname, '|' ORDER BY policyname)), '-') FROM pg_policies WHERE schemaname = 'app';
SELECT 'alembic', count(*), coalesce(md5(string_agg(version_num, '|')), '-') FROM alembic_version;
SQL
  # Drill helpers reuse the db image as root; production uses alpine the
  # same way, the image just happens to already be on the box here.
  docker run --rm --user root -v drill_blobs:/blobs:ro $IMG bash -c \
    "cd /blobs && find . -type f | sort | xargs -r sha256sum"
}

step "1. clean slate"
cleanup
mkdir -p "$DRILL/backups"

step "2. stand up 'production' (prod image + db-init, like compose)"
start_db

step "3. real migrations (alembic upgrade head)"
cd "$REPO/backend"
JBRAIN_MIGRATION_DATABASE_URL="postgresql+asyncpg://jbrain:$PGPW@localhost:5432/jbrain" \
  uv run alembic upgrade head

step "4. seed notes/attachments/chunks across domains + blob files"
psql_super <<'SQL'
INSERT INTO app.subjects (id, display_name, kind)
VALUES ('00000000-0000-0000-0000-000000000001', 'Owner', 'person');
INSERT INTO app.principals (id, kind, subject_id, key_hash, label)
VALUES ('00000000-0000-0000-0000-000000000002', 'owner',
        '00000000-0000-0000-0000-000000000001', 'drill-hash', 'drill');
INSERT INTO app.notes (id, client_id, domain_code, body) VALUES
 ('10000000-0000-0000-0000-000000000001', 'c1', 'general', 'grocery run notes'),
 ('10000000-0000-0000-0000-000000000002', 'c2', 'health',  'bp 120/80 this morning'),
 ('10000000-0000-0000-0000-000000000003', 'c3', 'finance', 'paid the water bill');
INSERT INTO app.attachments (id, note_id, domain_code, sha256, filename, media_type, size_bytes) VALUES
 ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000002',
  'health', 'aaaa1111', 'labs.pdf', 'application/pdf', 11),
 ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001',
  'general', 'bbbb2222', 'list.txt', 'text/plain', 9);
INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text, embedding, embedding_model)
SELECT gen_random_uuid(), n.id, n.domain_code, 'paragraph', 1, n.body,
       ('[' || (SELECT string_agg(((s * 7 % 13)::float / 13)::text, ',')
                FROM generate_series(1, 384) s) || ']')::vector,
       'bge-small-en-v1.5'
FROM app.notes n;
SQL
docker run --rm --user root -v drill_blobs:/blobs $IMG bash -c \
  "mkdir -p /blobs/aa /blobs/bb && printf 'fake pdf!!\n' > /blobs/aa/aaaa1111 && printf 'milk eggs\n' > /blobs/bb/bbbb2222"

step "5. fingerprint 'production' state"
fingerprint | tee "$DRILL/before.txt"

step "6. backup (backup.sh's exact commands)"
docker exec drill-db pg_dump -U jbrain -Fc jbrain > "$DRILL/backups/jbrain-drill.dump"
docker run --rm --user root -v drill_blobs:/blobs:ro -v "$DRILL/backups:/out" $IMG \
  tar czf /out/blobs-drill.tar.gz -C /blobs .

step "7. DISASTER — destroy the container, its data, and the blob volume"
cleanup

step "8. fresh stack (what install.sh gives you on a new box)"
start_db

step "9. restore (restore.sh's exact commands)"
docker exec -i drill-db pg_restore -U jbrain -d jbrain \
  --clean --if-exists --exit-on-error < "$DRILL/backups/jbrain-drill.dump"
docker run --rm --user root -v drill_blobs:/blobs -v "$DRILL/backups:/in:ro" $IMG bash -c \
  "find /blobs -mindepth 1 -delete && tar xzf /in/blobs-drill.tar.gz -C /blobs"

step "10. verify: fingerprints match"
fingerprint | tee "$DRILL/after.txt"
diff "$DRILL/before.txt" "$DRILL/after.txt"
echo "fingerprints match"

step "11. verify: RLS + least privilege survived for jbrain_app"
export PGPASSWORD=$APPPW
owner=$(app_count <<'SQL'
BEGIN;
SELECT set_config('app.principal_kind', 'owner', true);
SELECT count(*) FROM app.notes;
COMMIT;
SQL
)
scoped=$(app_count <<'SQL'
BEGIN;
SELECT set_config('app.principal_kind', 'capability_token', true);
SELECT set_config('app.domain_scopes', 'health', true);
SELECT count(*) FROM app.notes;
COMMIT;
SQL
)
noctx=$(psql -h localhost -U jbrain_app -d jbrain -tA -c "SELECT count(*) FROM app.notes;")
ddl_denied=true
psql -h localhost -U jbrain_app -d jbrain -c "DROP TABLE app.notes;" >/dev/null 2>&1 && ddl_denied=false
echo "owner sees $owner/3, health-scoped sees $scoped/1, no-context sees $noctx/0, DDL denied: $ddl_denied"
[ "$owner" = 3 ] && [ "$scoped" = 1 ] && [ "$noctx" = 0 ] && [ "$ddl_denied" = true ]

step "DRILL PASSED"
