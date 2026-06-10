FROM python:3.12-slim AS base

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
# Pre-own the blob mount point so the named volume inherits write access.
RUN mkdir -p /data/blobs && chown appuser /data/blobs
USER appuser

EXPOSE 8000
CMD ["uvicorn", "jbrain.main:app", "--host", "0.0.0.0", "--port", "8000"]
