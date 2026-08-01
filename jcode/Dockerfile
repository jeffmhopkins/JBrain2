# The jcode control server: a Python service (mirroring the supervisor) that
# manages sandboxed coding sessions driven through an interactive terminal. Each
# session is a git checkout with a shell; the owner runs xAI's official Grok Build
# CLI (`grok`) against an on-box coder model. `grok` is a standalone binary (no
# runtime) that reaches the model over an OpenAI-compatible base_url
# (~/.grok/config.toml, written from env by /etc/profile.d/grok-config.sh) straight at
# the gateway — local, no cloud. `git` handles the per-session checkouts; Node/npm are
# kept for in-sandbox JavaScript tooling the owner may run.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

# git: per-session checkouts. nodejs/npm: in-sandbox JavaScript tooling (not the harness).
# Grok Build (`grok`) is xAI's official agent — a standalone precompiled binary (no Node
# or Bun), installed below into /usr/local/bin (on PATH for the login shell). The
# per-session web preview is host-served (no `cloudflared` in the image since the Wave P5b
# cutover — docs/archive/JCODE_PREVIEW_HOST_PLAN.md).
# Grok Build, pinned (provenance matters — a remote binary). The latest stable is the
# https://x.ai/cli/stable pointer; bump deliberately.
ARG GROK_BUILD_VERSION=0.2.67
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && GROK_BIN_DIR=/usr/local/bin curl -fsSL https://x.ai/cli/install.sh | bash -s -- "${GROK_BUILD_VERSION}" \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Login-shell hook: write ~/.grok/config.toml from the GROK_* env so `grok` targets the
# on-box coder model (and the per-session model pin), not xAI's cloud. /etc/profile.d is
# sourced by the interactive `/bin/bash -l`.
COPY grok-config.sh /etc/profile.d/grok-config.sh

# Login-shell hook: re-prepend the session's private tool dirs ($JCODE_TOOLS_BIN, set per
# session by the terminal) to PATH after /etc/profile resets root's PATH, so a per-session
# tool install (`jcode-grok upgrade`) shadows the image's copy. See
# docs/archive/JCODE_SESSION_TOOLS_PLAN.md.
COPY jcode-path.sh /etc/profile.d/jcode-path.sh

# Per-session grok version management: `jcode-grok upgrade` installs grok into the session's
# private $JCODE_TOOLS_BIN (shadowing the pinned image copy for that session only); the image
# default stays pinned at GROK_BUILD_VERSION. On the shared PATH so every session shell has it.
COPY jcode-grok /usr/local/bin/jcode-grok
# SearXNG-backed web search/fetch for the session CLIs (docs/plans/JCODE_GROK_INTERNET_PLAN.md).
# On the shared PATH; each is a no-op that self-explains unless the session opted in
# (JCODE_INTERNET_SEARCH=1, set by the terminal from the owner's checkbox). They reach the
# api's jcode bridge — the sandbox can't touch searxng directly.
COPY web-search /usr/local/bin/web-search
COPY web-fetch /usr/local/bin/web-fetch
# Deterministic OCR of a local image/PDF (docs/plans/RAPIDOCR_PLAN.md). Always available —
# an offline read via the api's OCR bridge, no internet opt-in to gate it.
COPY ocr /usr/local/bin/ocr
RUN chmod +x /usr/local/bin/jcode-grok \
    /usr/local/bin/web-search /usr/local/bin/web-fetch /usr/local/bin/ocr

# Login-shell hint: announce the web helpers in a session that opted into web search, so the
# owner (and the agent's own shell output) sees the capability. Silent otherwise.
COPY web-tools.sh /etc/profile.d/jcode-web.sh

# Login-shell hook: write an AGENTS.md / CLAUDE.md to the workspace root so the in-sandbox
# grok/claude actually DISCOVER the shell tools (they read those memory files, not the login
# banner). Above every checkout, so it never dirties the owner's repo.
COPY agents-md.sh /etc/profile.d/jcode-agents.sh

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Layered install: deps first so source edits don't bust the dependency cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# The per-session git checkouts live here (the `jcode_work` volume mounts over it).
RUN mkdir -p /work
ENV JCODE_WORKSPACE_ROOT=/work

EXPOSE 9100

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/healthz', timeout=3).status == 200 else 1)"]

CMD ["/app/.venv/bin/uvicorn", "jcode_ctl.main:app", "--host", "0.0.0.0", "--port", "9100"]
