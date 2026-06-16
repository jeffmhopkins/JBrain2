"""The note.extract eval corpus + scorer, shipped IN the `jbrain` package.

The runtime-needed pieces — the curated case fixtures (`cases/*.json`) and the
scoring core (`runner.py`) — live here, inside the installed package, so the
nightly `eval_run` workflow (Phase-5 Track H·B) can score the live model in
PRODUCTION (the container image ships `src/jbrain`). The dev-only CLI wrapper
(`backend/evals/run.py`, `backend/evals/audit.py`) imports its core from here.
"""
