#!/usr/bin/env bash
# Opt-in LLM-in-the-loop eval of the note.extract prompt (NOT run in CI — it
# calls a real model via the LLM adapter). Scores the model's own output for
# the object-person and backward-temporal lapses note-extract-v5 targets.
# Routes to whatever provider/model your config points note.extract at
# (JBRAIN_LLM_TASKS + provider keys / base URLs). See backend/evals/run.py.
#
#   scripts/prompt-eval.sh                       # all cases
#   scripts/prompt-eval.sh --strict              # exit 1 if any case fails
#   scripts/prompt-eval.sh --case marriage_copular_object
set -euo pipefail
cd "$(dirname "$0")/../backend"
exec uv run python -m evals.run "$@"
