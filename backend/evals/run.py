"""LLM-in-the-loop eval CLI for the note.extract prompt (opt-in, NOT run in CI).

Unlike the deterministic harness (tests/harness — which scripts a perfect model
and exercises the pipeline), this runs the REAL system prompt through a REAL
model via the LLM adapter and scores the model's own output. It is how a prompt
change (e.g. note-extract-v5's object-person + backward-temporal guidance) is
MEASURED rather than guessed — the gap the harness explicitly cannot cover
("does not test the prompt — only a live model exercises that").

The scoring core (`load_cases`/`score_cases`/`eval_run_from_cases` + the case
fixtures) lives in the shipped package `jbrain.evals.runner`, so the same code
the nightly `eval_run` action runs in production is what this CLI exercises. This
file is just the command-line glue (argument parsing + the human report).

It routes to whatever provider/model your config points note.extract at
(JBRAIN_LLM_TASKS, provider keys / base URLs), so the same cases score Claude,
grok, or a local model. CI never calls a live model, so this lives outside the
test suite. ONE COMMAND, then copy the whole report back:

    scripts/prompt-eval.sh           # all cases (failures dump the raw output)
    scripts/prompt-eval.sh --strict  # exit 1 if any case fails
    scripts/prompt-eval.sh --case marriage_copular_object
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from typing import Any

from jbrain.analysis.prompt import NOTE_EXTRACT_STRENGTH, PROMPT_VERSION
from jbrain.config import Settings
from jbrain.evals.runner import CaseResult, load_cases, score_cases
from jbrain.llm import build_router


async def _run(cases: list[dict[str, Any]]) -> list[CaseResult]:
    # Parse WITH the anchor, exactly as the pipeline does for a note whose client
    # offset is known (see score_cases) — a green eval then means a green app.
    router = build_router(Settings())
    provider, model = router.spec("note.extract", NOTE_EXTRACT_STRENGTH)
    print(
        f"prompt-eval — {provider}:{model} — {PROMPT_VERSION} — "
        f"{datetime.now().isoformat(timespec='seconds')}",
        flush=True,
    )
    print("-" * 64, flush=True)
    results, _tokens = await score_cases(router, cases, echo=True)
    return results


def _report(results: list[CaseResult]) -> bool:
    checks_total = sum(len(r.checks) for r in results)
    checks_ok = sum(ok for r in results for _, ok, _ in r.checks)
    passed = sum(r.passed for r in results)
    pct = (100 * checks_ok / checks_total) if checks_total else 0.0
    print("-" * 64, flush=True)
    print(
        f"{passed}/{len(results)} cases passed; {checks_ok}/{checks_total} checks ({pct:.0f}%)",
        flush=True,
    )
    # The "leaner" metric (docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md §7): total facts the
    # model emitted across the corpus — compare before/after a salience change.
    print(f"corpus-total facts emitted: {sum(r.fact_count for r in results)}", flush=True)
    return passed == len(results)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", help="run only the named case")
    ap.add_argument(
        "--like", help="run only cases whose name contains any of these (comma-separated)"
    )
    ap.add_argument("--strict", action="store_true", help="exit 1 unless every case passes")
    args = ap.parse_args()

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
    if args.like:
        wants = [w.strip() for w in args.like.split(",") if w.strip()]
        cases = [c for c in cases if any(w in c["name"] for w in wants)]
    if not cases:
        print("no matching cases", file=sys.stderr)
        return 2
    results = asyncio.run(_run(cases))
    all_passed = _report(results)
    return 0 if (all_passed or not args.strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
