"""CLI for the real-Grok eval: `uv run python -m tests.eval.run [id-filter]`.

The opt-in quality gate to run BEFORE shipping a prompt change. Needs
JBRAIN_XAI_API_KEY. Prints per-case PASS / FAIL / ADVISORY, the failures, and the
token cost. Exit code is non-zero if any non-advisory case fails — so it can gate
a release. Advisory cases (genuinely debatable "correct" answer) report but never
fail the run.
"""

from __future__ import annotations

import asyncio
import sys

from jbrain.config import Settings
from jbrain.llm import build_router
from jbrain.llm.types import LlmUsage
from tests.eval.assertions import check_case
from tests.eval.cases import load_corpus
from tests.eval.runner import run_case

_PRICE_IN, _PRICE_OUT = 1.25, 2.50  # $/M tokens, grok-4.3


class _Tally:
    def __init__(self) -> None:
        self.inp = self.out = self.calls = 0

    async def record(self, *, task: str, provider: str, model: str, usage: LlmUsage) -> None:
        self.inp += usage.input_tokens
        self.out += usage.output_tokens
        self.calls += 1


async def main() -> int:
    settings = Settings()
    if not settings.xai_api_key:
        print("JBRAIN_XAI_API_KEY not set — the real-Grok eval is opt-in.")
        return 2
    flt = sys.argv[1] if len(sys.argv) > 1 else ""
    cases = [c for c in load_corpus() if not flt or flt in c.id]
    tally = _Tally()
    router = build_router(settings, recorder=tally)

    failed: list[str] = []
    advisory_failed: list[str] = []
    for case in cases:
        try:
            intent, plan = await run_case(router, case)
            fails = check_case(case, intent, plan)
        except Exception as exc:  # noqa: BLE001 - the eval surfaces whatever happens
            fails = [f"RAISED {type(exc).__name__}: {exc}"]
        tag = "ADVISORY" if case.advisory else ("FAIL" if fails else "PASS")
        if fails and case.advisory:
            advisory_failed.append(case.id)
        elif fails:
            failed.append(case.id)
        marker = "ok " if not fails else "XX "
        print(f"{marker}[{tag:8}] {case.id} ({case.category})")
        for f in fails:
            print(f"      - {f}")

    cost = tally.inp / 1e6 * _PRICE_IN + tally.out / 1e6 * _PRICE_OUT
    print(
        f"\n{len(cases)} cases · {len(failed)} hard-fail · {len(advisory_failed)} advisory-miss"
        f" · {tally.calls} calls ~${cost:.3f}"
    )
    if failed:
        print("HARD FAILURES:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
