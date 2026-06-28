#!/usr/bin/env bash
# JBrain2 installer: barebones Ubuntu -> running stack, built from source.
#
# From a clone:   sudo bash deploy/install.sh
# Or piped:       curl -fsSL https://raw.githubusercontent.com/jeffmhopkins/JBrain2/main/deploy/install.sh | sudo bash
#
# The stack builds its images from /opt/jbrain2/src (a git clone), so nothing
# is pulled from a registry except public base images. Idempotent: re-running
# refreshes helper scripts but never overwrites an existing .env or src tree.
set -euo pipefail

REPO_URL="https://github.com/jeffmhopkins/JBrain2.git"
INSTALL_DIR="/opt/jbrain2"
# Resolves to the deploy/ dir when run from a clone; empty when piped.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

say() { printf '\n[jbrain install] %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker Engine"
  curl -fsSL https://get.docker.com | sh
fi
if ! command -v git >/dev/null 2>&1; then
  say "Installing git"
  apt-get update -qq && apt-get install -y -qq git
fi
# python3 is used by the opt-in local-model setup (scripts/local-llm-setup.sh).
if ! command -v python3 >/dev/null 2>&1; then
  say "Installing python3"
  apt-get update -qq && apt-get install -y -qq python3
fi

say "Setting up $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/backups" "$INSTALL_DIR/db-init" "$INSTALL_DIR/searxng"

if [ ! -d "$INSTALL_DIR/src/.git" ]; then
  if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/install.sh" ] && [ -d "$SRC_DIR/../.git" ]; then
    REPO_ROOT="$(cd "$SRC_DIR/.." && pwd)"
    say "Copying source tree from $REPO_ROOT"
    cp -a "$REPO_ROOT" "$INSTALL_DIR/src"
  else
    say "Cloning source tree"
    git clone "$REPO_URL" "$INSTALL_DIR/src"
  fi
fi

cd "$INSTALL_DIR"
for f in docker-compose.yml jbrain backup.sh restore.sh; do
  cp "src/deploy/$f" "$f"
done
cp src/deploy/db-init/01-app-role.sh db-init/
# The SearXNG settings live in a host file because compose bind-mounts it
# writable (the image injects $SEARXNG_SECRET into it at boot) and it enables
# the JSON format the web_search tool needs. Without this copy the bind source
# is missing, Docker creates an empty dir in its place, SearXNG falls back to
# its HTML-only defaults, and /search?format=json answers 403 — jerv then
# reports web search as unavailable. rm first: on a box already broken this way
# the path is that Docker-made directory, and `cp file dir/` would drop the file
# inside it rather than replace it, leaving the dir mount in place.
rm -rf searxng/settings.yml
cp src/deploy/searxng/settings.yml searxng/settings.yml
chmod +x jbrain backup.sh restore.sh db-init/01-app-role.sh
ln -sf "$INSTALL_DIR/jbrain" /usr/local/bin/jbrain

if [ ! -f .env ]; then
  say "First-time configuration"
  read -rp "Domain for this server (e.g. brain.example.com): " DOMAIN

  # Access mode decides how the world reaches this box. Tunnel mode is the
  # default because it needs no static IP, no port-forwarding, and survives
  # CGNAT — the common home-network case. Direct mode is for a box that already
  # has a public name resolving to it with inbound 80/443 open (Let's Encrypt).
  echo
  echo "How is this box reached on your network?"
  echo "  1) Cloudflare Tunnel — recommended for home/dynamic-IP; no port-forwarding, works behind CGNAT"
  echo "  2) Direct — this box has a public name + inbound 80/443 (Caddy gets Let's Encrypt)"
  read -rp "Choose [1/2] (default 1): " ACCESS_CHOICE
  if [ "${ACCESS_CHOICE:-1}" = "2" ]; then
    SITE_ADDR="$DOMAIN"          # Caddy fetches Let's Encrypt; needs inbound 80/443.
    TUNNEL_ENABLED=false
    TUNNEL_TOKEN=""
  else
    SITE_ADDR="http://$DOMAIN"   # Cloudflare terminates TLS; Caddy serves plain HTTP.
    TUNNEL_ENABLED=true
    echo
    echo "Cloudflare Tunnel token — create the tunnel under Cloudflare Zero Trust"
    echo "(Networks > Tunnels > Create) and paste the token it shows (starts 'eyJ')."
    echo "Leave blank to add CLOUDFLARE_TUNNEL_TOKEN to $INSTALL_DIR/.env later."
    echo "Full walkthrough: docs/CLOUDFLARE_TUNNEL.md"
    read -rp "Cloudflare Tunnel token: " TUNNEL_TOKEN
  fi

  # Local-network access is ON by default: the box answers at jbrain.local over
  # HTTPS (Caddy's internal CA) so the owner can sign in from the LAN even when
  # the tunnel/internet is down (the Secure cookie needs HTTPS). The host half —
  # mDNS + the CNAME alias that makes the name resolve — is set up by lan-setup.sh
  # after the stack is up. Edit JBRAIN_LAN_ADDR in .env to rename or disable it.
  LAN_ADDR="https://jbrain.local"

  read -rp "Anthropic API key (blank to skip): " ANTHROPIC_KEY
  read -rp "xAI API key (blank to skip): " XAI_KEY
  # Self-hosted local models are off by default; offer them only on capable
  # hardware (AMD Strix Halo class GPU). Provisioning is deferred to after the
  # stack is up because it downloads tens of GB of weights.
  read -rp "Enable self-hosted local models? Needs an AMD GPU + lots of RAM [y/N]: " LOCAL_CHOICE

  cat > .env <<EOF
JBRAIN_DOMAIN=$DOMAIN
JBRAIN_SITE_ADDR=$SITE_ADDR
JBRAIN_LAN_ADDR=$LAN_ADDR
TUNNEL_ENABLED=$TUNNEL_ENABLED
CLOUDFLARE_TUNNEL_TOKEN=$TUNNEL_TOKEN
POSTGRES_PASSWORD=$(openssl rand -hex 32)
APP_DB_PASSWORD=$(openssl rand -hex 32)
SUPERVISOR_TOKEN=$(openssl rand -hex 32)
SEARXNG_SECRET=$(openssl rand -hex 32)
ANTHROPIC_API_KEY=$ANTHROPIC_KEY
XAI_API_KEY=$XAI_KEY
EOF
  chmod 600 .env
else
  say "Existing .env found — keeping current configuration and secrets"
  # Backfill SEARXNG_SECRET for installs that predate the web-search service:
  # SearXNG refuses to start without one, which would leave web_search reporting
  # the service as unavailable. Append only when absent so existing secrets stand.
  if ! grep -q '^SEARXNG_SECRET=' .env; then
    say "Adding SEARXNG_SECRET for web search"
    printf 'SEARXNG_SECRET=%s\n' "$(openssl rand -hex 32)" >> .env
  fi
  # Turn LAN access on for installs that predate it (compose also defaults this,
  # but writing it keeps the knob discoverable + editable). Absent only.
  if ! grep -q '^JBRAIN_LAN_ADDR=' .env; then
    say "Enabling LAN access (https://jbrain.local)"
    printf 'JBRAIN_LAN_ADDR=%s\n' "https://jbrain.local" >> .env
  fi
fi

# Bring up the opt-in tunnel connector when the operator chose it. Read from
# .env (not COMPOSE_PROFILES) so the choice survives re-runs, mirroring how the
# `jbrain` helper resolves profiles.
PROFILE=()
if grep -q '^TUNNEL_ENABLED=true' .env; then
  PROFILE+=(--profile tunnel)
fi

say "Building images from source (a few minutes on first run)"
docker compose build

say "Starting database"
docker compose up -d db
until docker compose exec -T db pg_isready -U jbrain -d jbrain >/dev/null 2>&1; do
  sleep 2
done

say "Running migrations"
docker compose run --rm migrate

say "Starting the stack"
docker compose "${PROFILE[@]}" up -d

if [ ! -f .owner-initialized ]; then
  say "Generating your owner key"
  docker compose run --rm api python -m jbrain.cli init
  touch .owner-initialized
fi

if [ "${LOCAL_CHOICE:-}" = "y" ] || [ "${LOCAL_CHOICE:-}" = "Y" ]; then
  say "Provisioning self-hosted local models (recommended set)"
  JBRAIN_INSTALL_DIR="$INSTALL_DIR" bash "$INSTALL_DIR/src/scripts/local-llm-setup.sh" \
    || say "Local model setup did not complete — run 'jbrain enable-local-models' later."
fi

# Host half of LAN access: mDNS + the CNAME alias that resolves JBRAIN_LAN_ADDR.
# Best-effort — a failure here never blocks the install (the box is still
# reachable via the tunnel/direct site).
if grep -q '^JBRAIN_LAN_ADDR=https' .env; then
  say "Setting up LAN access (mDNS + jbrain.local alias)"
  JBRAIN_INSTALL_DIR="$INSTALL_DIR" bash "$INSTALL_DIR/src/deploy/lan-setup.sh" \
    || say "LAN setup did not complete — run 'jbrain enable-lan' later."
fi

say "Installing nightly backup (03:30)"
cat > /etc/cron.d/jbrain-backup <<EOF
30 3 * * * root $INSTALL_DIR/backup.sh >> $INSTALL_DIR/backups/backup.log 2>&1
EOF

DONE_DOMAIN="$(grep '^JBRAIN_DOMAIN=' .env | cut -d= -f2)"
if grep -q '^TUNNEL_ENABLED=true' .env; then
  say "Done. Finish the Cloudflare side (docs/CLOUDFLARE_TUNNEL.md): add a public"
  say "hostname for $DONE_DOMAIN routing to http://proxy:80, then open"
  say "https://$DONE_DOMAIN and paste your owner key."
else
  say "Done. Open https://$DONE_DOMAIN and paste your owner key."
fi
DONE_LAN="$(grep '^JBRAIN_LAN_ADDR=' .env | cut -d= -f2)"
if [ -n "$DONE_LAN" ]; then
  say "On the same network you can also use $DONE_LAN (trust the local"
  say "certificate on first visit — see docs/LOCAL_ACCESS.md)."
fi
say "Manage with: jbrain status | restart | logs | reset-owner-key | update | enable-lan | backup | restore"
