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

RUN useradd --create-home appuser
# Pre-own the mount points so the named volumes inherit write access. Without
# this the volume mounts as root and the non-root app can't write — the tile
# cache in particular would fail every write, blanking the location basemap.
RUN mkdir -p /data/blobs /data/tiles && chown appuser /data/blobs /data/tiles
USER appuser

EXPOSE 8000
CMD ["uvicorn", "jbrain.main:app", "--host", "0.0.0.0", "--port", "8000"]
