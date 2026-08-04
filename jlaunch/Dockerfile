# The jlaunch control server: a Python service (mirroring jcode/the supervisor) that
# runs a registered long-running one-shot computation as a supervised job. A job clones
# its repo, builds its own venv, and runs a phased shell pipeline in a PTY streamed to the
# owner's browser. `git` clones the compute repo; `python3-venv` + `build-essential` let a
# job build numpy/sympy and compile any source deps. Unlike jcode, no coding-agent CLI is
# installed — the "shell" here is the job script itself.
#
# Build context is the REPO ROOT (compose `context: ./src`) so the es-census census
# tool (research/es-census) can be compiled and baked in for the native Erdős–Straus
# rerun spec. A throwaway Rust stage compiles it; only the ~few-MB binary is copied into
# the runtime image (the toolchain is discarded).

# --- es-census build (native Erdős–Straus census, baked in for the launcher) ---
FROM rust:1-slim AS escensus
WORKDIR /build/es-census
COPY research/es-census/Cargo.toml research/es-census/Cargo.lock ./
COPY research/es-census/src ./src
RUN cargo build --release --locked

# --- jlaunch control server ---
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/
# The native census binary (no runtime deps; statically links its Rust crates).
COPY --from=escensus /build/es-census/target/release/es-census /usr/local/bin/es-census

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates python3-venv build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Layered install: deps first so source edits don't bust the dependency cache. Paths are
# repo-root-relative because the build context is the repo root.
COPY jlaunch/pyproject.toml jlaunch/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY jlaunch/src ./src
RUN uv sync --frozen --no-dev

# The per-job checkouts + ~15 GB scratch live here (the `jlaunch_work` volume mounts over
# it); collected artifacts land on the `jlaunch_artifacts` volume.
RUN mkdir -p /work /artifacts
ENV JLAUNCH_WORKSPACE_ROOT=/work \
    JLAUNCH_ARTIFACT_ROOT=/artifacts

EXPOSE 9101

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9101/healthz', timeout=3).status == 200 else 1)"]

CMD ["/app/.venv/bin/uvicorn", "jlaunch_ctl.main:app", "--host", "0.0.0.0", "--port", "9101"]
