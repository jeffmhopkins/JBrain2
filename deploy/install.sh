#!/usr/bin/env bash
# JBrain2 installer: barebones Ubuntu -> running stack.
#
# From a clone:   sudo bash deploy/install.sh
# Or piped:       curl -fsSL https://raw.githubusercontent.com/jeffmhopkins/JBrain2/main/deploy/install.sh | sudo bash
#
# Idempotent: re-running updates the helper scripts but never overwrites an
# existing .env or regenerates secrets.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/jeffmhopkins/JBrain2/main/deploy"
INSTALL_DIR="/opt/jbrain2"
# Resolves to the deploy/ dir when run from a clone; empty-ish when piped.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

say() { printf '\n[jbrain install] %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker Engine"
  curl -fsSL https://get.docker.com | sh
fi

say "Setting up $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/db-init" "$INSTALL_DIR/backups"
cd "$INSTALL_DIR"

for f in docker-compose.yml jbrain backup.sh db-init/01-app-role.sh; do
  if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/$f" ]; then
    cp "$SRC_DIR/$f" "$f"
  else
    curl -fsSL "$REPO_RAW/$f" -o "$f"
  fi
done
chmod +x jbrain backup.sh db-init/01-app-role.sh
ln -sf "$INSTALL_DIR/jbrain" /usr/local/bin/jbrain

if [ ! -f .env ]; then
  say "First-time configuration"
  read -rp "Domain for this server (e.g. brain.example.com): " DOMAIN
  read -rp "Anthropic API key (blank to skip): " ANTHROPIC_KEY
  read -rp "xAI API key (blank to skip): " XAI_KEY

  cat > .env <<EOF
JBRAIN_DOMAIN=$DOMAIN
JBRAIN_TAG=stable
POSTGRES_PASSWORD=$(openssl rand -hex 32)
APP_DB_PASSWORD=$(openssl rand -hex 32)
SUPERVISOR_TOKEN=$(openssl rand -hex 32)
ANTHROPIC_API_KEY=$ANTHROPIC_KEY
XAI_API_KEY=$XAI_KEY
EOF
  chmod 600 .env
else
  say "Existing .env found — keeping current configuration and secrets"
fi

say "Pulling images"
docker compose pull

say "Starting database"
docker compose up -d db
until docker compose exec -T db pg_isready -U jbrain -d jbrain >/dev/null 2>&1; do
  sleep 2
done

say "Running migrations"
docker compose run --rm api alembic upgrade head

say "Starting the stack"
docker compose up -d

if [ ! -f .owner-initialized ]; then
  say "Generating your owner key"
  docker compose run --rm api python -m jbrain.cli init
  touch .owner-initialized
fi

say "Installing nightly backup (03:30)"
cat > /etc/cron.d/jbrain-backup <<EOF
30 3 * * * root $INSTALL_DIR/backup.sh >> $INSTALL_DIR/backups/backup.log 2>&1
EOF

say "Done. Open https://$(grep '^JBRAIN_DOMAIN=' .env | cut -d= -f2) and paste your owner key."
say "Manage with: jbrain status | restart | logs | reset-owner-key | update | backup"
