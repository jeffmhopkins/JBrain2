FROM python:3.12-slim AS base

# ffmpeg/ffprobe: the worker's analyze_video job samples video frames and extracts
# the audio track through them (jbrain.media). A slim base has neither.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Build provenance: the git revision + build time of the source COPY'd above, so
# the running server can report exactly what is deployed (debug /version). Set by
# deploy/update-inner.sh via compose build args; "unknown" on a plain local build.
# Placed after the heavy layers so re-stamping a new SHA rebuilds only these
# trivial trailing layers, not the uv sync.
ARG JBRAIN_GIT_SHA=unknown
ARG JBRAIN_GIT_DESCRIBE=unknown
ARG JBRAIN_BUILD_TIME=unknown
ENV JBRAIN_GIT_SHA=$JBRAIN_GIT_SHA \
    JBRAIN_GIT_DESCRIBE=$JBRAIN_GIT_DESCRIBE \
    JBRAIN_BUILD_TIME=$JBRAIN_BUILD_TIME

# Pinned uid: the host bind-mounts the local-models dir into this container, and
# scripts/local-llm-setup.sh chowns that host dir to this same uid so the non-root
# app can re-stamp llama-swap.yaml on a settings change (staging, a context-window
# edit). A drifting uid would silently re-break that write, so fix it here.
RUN useradd --create-home --uid 1000 appuser
# Pre-own the mount points so the named volumes inherit write access. Without
# this the volume mounts as root and the non-root app can't write — the tile
# cache in particular would fail every write, blanking the location basemap.
RUN mkdir -p /data/blobs /data/tiles && chown appuser /data/blobs /data/tiles
USER appuser

EXPOSE 8000
CMD ["uvicorn", "jbrain.main:app", "--host", "0.0.0.0", "--port", "8000"]
