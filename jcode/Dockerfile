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
# Session shells are not root (each session runs as its own uid), and the installer keeps
# the real binary under $HOME/.grok and only symlinks it onto PATH — so installed with
# HOME=/root, /usr/local/bin/grok would point into a 0700 directory no session can enter.
# Install into a throwaway HOME and copy the binaries themselves into /usr/local/bin.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://x.ai/cli/install.sh -o /tmp/grok-install.sh \
    && HOME=/tmp/grok-home GROK_BIN_DIR=/usr/local/bin \
        bash /tmp/grok-install.sh "${GROK_BUILD_VERSION}" \
    && for b in grok agent; do \
        if [ -L "/usr/local/bin/$b" ]; then \
          cp --remove-destination "$(readlink -f "/usr/local/bin/$b")" "/usr/local/bin/$b"; \
        fi; \
        if [ -e "/usr/local/bin/$b" ]; then chmod 0755 "/usr/local/bin/$b"; fi; \
      done \
    && rm -rf /tmp/grok-home /tmp/grok-install.sh \
    && [ ! -L /usr/local/bin/grok ] && [ -x /usr/local/bin/grok ] \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# The session uid pool (jcode_ctl.sandbox.UID_LOW..UID_HIGH): each live session runs as
# one of these. Registered so tools that look the user up by uid — git's ident, Node's
# os.userInfo(), ssh — find a name instead of failing. No password, no real home: the
# control server sets HOME per session. test_sandbox pins this range to the code's.
RUN for id in $(seq 20000 20999); do \
      echo "jcode${id}:x:${id}:${id}:jcode session:/nonexistent:/bin/bash" >> /etc/passwd; \
      echo "jcode${id}:x:${id}:" >> /etc/group; \
    done

# Login-shell hook: write ~/.grok/config.toml from the GROK_* env so `grok` targets the
# on-box coder model (and the per-session model pin), not xAI's cloud. /etc/profile.d is
# sourced by the interactive `/bin/bash -l`.
COPY grok-config.sh /etc/profile.d/grok-config.sh

# Login-shell hook: re-prepend the session's private tool dirs ($JCODE_TOOLS_BIN, set per
# session by the terminal) to PATH after /etc/profile resets PATH, so a per-session
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
# 0755 / 0644 explicitly, not +x: the files keep the build checkout's modes otherwise, and a
# session's unprivileged uid needs to read and run every one of them.
RUN chmod 0755 /usr/local/bin/jcode-grok \
    /usr/local/bin/web-search /usr/local/bin/web-fetch /usr/local/bin/ocr \
    && chmod 0644 /etc/profile.d/grok-config.sh /etc/profile.d/jcode-path.sh

# Login-shell hint: announce the web helpers in a session that opted into web search, so the
# owner (and the agent's own shell output) sees the capability. Silent otherwise.
COPY web-tools.sh /etc/profile.d/jcode-web.sh
RUN chmod 0644 /etc/profile.d/jcode-web.sh

# The workspace root's AGENTS.md / CLAUDE.md (how the in-sandbox grok/claude discover the
# shell tools) is written by the control server at startup, as root — not by a login hook,
# since a session shell can't write /work (jcode_ctl.agent_files).

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Layered install: deps first so source edits don't bust the dependency cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# The per-session git checkouts live here (the `jcode_work` volume mounts over it). Root
# 0711: a session reaches its own tree by name but can't list or write beside it. The
# control server re-asserts this on every start, since the volume may predate it.
RUN mkdir -p /work && chmod 0711 /work
ENV JCODE_WORKSPACE_ROOT=/work

EXPOSE 9100

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/healthz', timeout=3).status == 200 else 1)"]

CMD ["/app/.venv/bin/uvicorn", "jcode_ctl.main:app", "--host", "0.0.0.0", "--port", "9100"]
