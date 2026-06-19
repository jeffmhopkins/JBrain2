#!/bin/sh
# Containerized `jbrain update`, launched by the supervisor as a detached
# one-shot (docker:cli image) so it survives the stack — including the
# supervisor itself — restarting beneath it. The project dir is mounted at
# its real host path, so compose's relative bind paths resolve correctly.
set -eu

echo "[update] starting"
./backup.sh || echo "[update] backup skipped (stack not fully up?)"

echo "[update] pulling latest main"
# The pull runs as root inside the ephemeral updater container, but the
# bind-mounted worktree is owned by the host operator's UID, so git's
# dubious-ownership guard aborts ("detected dubious ownership"). Mark the
# worktree safe for the container's root user — a host-side `safe.directory`
# never reaches here, since root's container HOME carries no gitconfig.
git config --global --add safe.directory "$PWD/src"
git -C src pull --ff-only

# Refresh host helper scripts from the updated tree (mv keeps any running
# reader on its old inode).
for f in docker-compose.yml backup.sh restore.sh jbrain; do
  cp "src/deploy/$f" "$f.new" && mv "$f.new" "$f"
done
cp src/deploy/db-init/01-app-role.sh db-init/
chmod +x jbrain backup.sh restore.sh db-init/01-app-role.sh

echo "[update] building images"
docker compose build

echo "[update] running migrations"
docker compose run --rm migrate

echo "[update] restarting stack"
docker compose up -d

docker image prune -f
docker builder prune -f --keep-storage 10GB >/dev/null 2>&1 || true
echo "[update] complete"
