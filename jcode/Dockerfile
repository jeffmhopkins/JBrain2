# The jcode control server: a Python service (mirroring the supervisor) that
# manages sandboxed coding sessions driven through an interactive terminal. Each
# session is a git checkout with a shell; the owner runs a coding CLI — `claude`
# (Claude Code) or `grok` (grok-dev) — against an on-box coder model. It needs Node +
# those CLIs and `git` for the per-session checkouts. `claude` reaches the model over
# ANTHROPIC_BASE_URL (the claude-shim translator); `grok`, being OpenAI-compatible,
# over GROK_BASE_URL straight at the gateway — both set by compose, no cloud.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/

# grok-dev's bin is `#!/usr/bin/env bun`, so the sandbox needs the Bun runtime. Install
# bun's global packages into /usr/local (already on PATH) so both `bun` and the `grok`
# shim land where the login shell finds them without a PATH edit.
ENV BUN_INSTALL=/usr/local

# git: per-session checkouts. nodejs/npm: `@anthropic-ai/claude-code` (`claude`) + `bun`,
# the runtime the Grok CLI needs. bun: installs `grok-dev` (`grok`) the way its docs
# prescribe (`bun add -g`), so its bun-native deps (@opentui) resolve correctly.
# cloudflared: the per-session web-preview quick-tunnel (Wave J4) — a single binary,
# no Cloudflare account/config (TryCloudflare). The release name matches dpkg's arch.
# PINNED to a fixed release (not `latest`) so the image is reproducible — this is the
# one binary that opens an outbound public tunnel, so its provenance matters (review
# SF3). Bump deliberately; verify the published checksum when you do.
ARG CLOUDFLARED_VERSION=2024.12.2
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code bun \
    && bun add -g grok-dev \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-$(dpkg --print-architecture)" -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

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
