#!/usr/bin/env bash
# Opt-in real-Grok quality gate for the note->graph pipeline. Run BEFORE shipping
# a prompt change (note.extract / integrate.note) or the weight/arbiter logic.
#
# Needs JBRAIN_XAI_API_KEY (the eval calls real Grok; ~$0.5 for the full corpus).
# Exits non-zero if any non-advisory case regresses, so it can gate a release.
#
#   scripts/grok-eval.sh              # full corpus
#   scripts/grok-eval.sh prod-bug     # only cases whose id contains "prod-bug"
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../backend"
export PATH="$HOME/.local/bin:$PATH"
exec uv run python -m tests.eval.run "$@"
