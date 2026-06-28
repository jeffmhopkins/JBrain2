# The jcode control server: a Python service (mirroring the supervisor) that
# manages sandboxed coding sessions driven through an interactive terminal. Each
# session is a git checkout with a shell; the owner runs a coding CLI — `claude`
# (Claude Code) or `grok` (xAI's official Grok Build) — against an on-box coder model. It
# needs Node (for `claude`) + `git` for the per-session checkouts; `grok` is a standalone
# binary (no runtime). `claude` reaches the model over ANTHROPIC_BASE_URL (the claude-shim
# translator); `grok` over an OpenAI-compatible base_url (~/.grok/config.toml, written from
# env by /etc/profile.d/grok-config.sh) straight at the gateway — both local, no cloud.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

# git: per-session checkouts. nodejs/npm: the `@anthropic-ai/claude-code` (`claude`) CLI.
# Grok Build (`grok`) is xAI's official agent — a standalone precompiled binary (no Node
# or Bun), installed below into /usr/local/bin (on PATH for the login shell). The
# per-session web preview is host-served (no `cloudflared` in the image since the Wave P5b
# cutover — docs/JCODE_PREVIEW_HOST_PLAN.md).
# Grok Build, pinned (provenance matters — a remote binary). The latest stable is the
# https://x.ai/cli/stable pointer; bump deliberately.
ARG GROK_BUILD_VERSION=0.2.67
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && GROK_BIN_DIR=/usr/local/bin curl -fsSL https://x.ai/cli/install.sh | bash -s -- "${GROK_BUILD_VERSION}" \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Login-shell hook: write ~/.grok/config.toml from the GROK_* env so `grok` targets the
# on-box coder model (and the per-session model pin), not xAI's cloud. /etc/profile.d is
# sourced by the interactive `/bin/bash -l`.
COPY grok-config.sh /etc/profile.d/grok-config.sh

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
