"""LLM-in-the-loop calibration for the wiki_lint (Wave B) verifier prompts.

Drives the REAL `wiki.lint.contradiction` / `wiki.lint.stale` system prompts + schemas (imported
from `jbrain.wiki.lint`, so a prompt edit is calibrated verbatim) over labelled cases and scores
precision/recall — the false-positive guard the disabled-in-prod state is waiting on
(docs/archive/WIKI_LINT_PLAN.md §7 criterion 8, §8 criterion 9).

The model is reached through an injected async `Completer(system, user_text, schema) -> parsed|None`
— all egress on the LLM adapter (non-neg #1). The `__main__` wires it to the owner debug console
(`/api/debug/complete`) using a capability token from the env, so calibration runs against the
model the owner's box actually serves the task with (no raw provider key needed here).

Run:  HB_URL=… HB_KEY=… uv run python -m jbrain.evals.wiki_lint_runner
      (optional WIKI_LINT_EVAL_TASK, default `wiki.ground` — a deployed high-effort verifier task;
       switch to `wiki.lint.contradiction` once Wave B is deployed and routed.)
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jbrain.wiki.lint import (
    _CONTRADICTION_SCHEMA,
    _STALE_SCHEMA,
    CONTRADICTION_SYSTEM,
    STALE_SYSTEM,
    contradiction_batch_line,
    stale_batch_line,
)

CASES_DIR = Path(__file__).parent / "wiki_lint_cases"

# Completer(system, user_text, json_schema) -> the parsed verdict object (or None on error).
Completer = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


def load_wiki_lint_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text()))
    return cases


@dataclass
class CaseResult:
    name: str
    kind: str
    should_fire: bool
    predicted: bool | None  # None = the verdict was unparseable / call failed
    summary: str

    @property
    def ok(self) -> bool:
        return self.predicted is not None and self.predicted == self.should_fire


async def _verdict_for(case: dict[str, Any], complete: Completer) -> tuple[bool | None, str]:
    """Drive one case through the real prompt (production wording via the shared line builders),
    single-item batch at index 0, and read its boolean verdict + summary."""
    if case["kind"] == "contradiction":
        user_text = contradiction_batch_line(0, case["a_claims"], case["b_claims"])
        parsed = await complete(CONTRADICTION_SYSTEM, user_text, _CONTRADICTION_SCHEMA)
        key = "contradiction"
    else:
        user_text = stale_batch_line(0, case["superseded_fact"], case["prose"])
        parsed = await complete(STALE_SYSTEM, user_text, _STALE_SCHEMA)
        key = "framed_as_current"
    if not isinstance(parsed, dict):
        return None, ""
    verdicts = parsed.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts or not isinstance(verdicts[0], dict):
        return None, ""
    v = verdicts[0]
    return bool(v.get(key) is True), str(v.get("summary", ""))


async def score_wiki_lint_cases(
    cases: list[dict[str, Any]], complete: Completer
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases:
        predicted, summary = await _verdict_for(case, complete)
        results.append(
            CaseResult(case["name"], case["kind"], bool(case["should_fire"]), predicted, summary)
        )
    return results


@dataclass
class Confusion:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    errors: int = 0

    def add(self, r: CaseResult) -> None:
        if r.predicted is None:
            self.errors += 1
        elif r.should_fire and r.predicted:
            self.tp += 1
        elif r.should_fire and not r.predicted:
            self.fn += 1
        elif not r.should_fire and r.predicted:
            self.fp += 1
        else:
            self.tn += 1

    @property
    def precision(self) -> float | None:
        d = self.tp + self.fp
        return None if d == 0 else self.tp / d

    @property
    def recall(self) -> float | None:
        d = self.tp + self.fn
        return None if d == 0 else self.tp / d


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def report(results: list[CaseResult]) -> bool:
    """Print per-case PASS/FAIL and the per-dimension confusion + precision/recall. Returns True
    when there are no hard failures (a FN or FP is a hard fail; an unparseable verdict a miss)."""
    by_kind: dict[str, Confusion] = {}
    for r in results:
        by_kind.setdefault(r.kind, Confusion()).add(r)
        tag = "PASS " if r.ok else ("ERROR" if r.predicted is None else "FAIL ")
        want = "fire" if r.should_fire else "quiet"
        got = "err" if r.predicted is None else ("fire" if r.predicted else "quiet")
        line = f"  {tag} [{r.kind[:5]}] {r.name}: want {want}, got {got}"
        if not r.ok and r.summary:
            line += f"  — model said: {r.summary}"
        print(line)
    print()
    ok = True
    for kind, c in sorted(by_kind.items()):
        print(
            f"{kind}: {c.tp}TP {c.fp}FP {c.fn}FN {c.tn}TN {c.errors}err"
            f"  · precision {_pct(c.precision)} · recall {_pct(c.recall)}"
        )
        if c.fp or c.fn or c.errors:
            ok = False
    total_fail = sum(1 for r in results if not r.ok)
    print(f"\n{len(results)} cases · {total_fail} miss(es)")
    return ok


class _GatewayCompleter:
    """Posts a completion to the owner debug console (`/api/debug/complete`) with a capability-token
    bearer key. Routes by `task` (the deployed instance's live routing decides the actual model)."""

    def __init__(self, base_url: str, key: str, task: str):
        import httpx

        self._url = f"{base_url.rstrip('/')}/api/debug/complete"
        self._headers = {"Authorization": f"Bearer {key}"}
        self._task = task
        self._client = httpx.AsyncClient(timeout=120)
        self.served: str | None = None

    async def __call__(
        self, system: str, user_text: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        body = {
            "task": self._task,
            "system": system,
            "user_text": user_text,
            "json_schema": schema,
            "max_tokens": 2048,
        }
        r = await self._client.post(self._url, headers=self._headers, json=body)
        if r.status_code != 200:
            print(f"  [gateway {r.status_code}] {r.text[:120]}")
            return None
        data = r.json()
        self.served = f"{data.get('provider')}:{data.get('model')}"
        parsed = data.get("parsed")
        return parsed if isinstance(parsed, dict) else None

    async def aclose(self) -> None:
        await self._client.aclose()


async def _main() -> int:
    base_url = os.environ.get("HB_URL")
    key = os.environ.get("HB_KEY")
    task = os.environ.get("WIKI_LINT_EVAL_TASK", "wiki.ground")
    if not base_url or not key:
        print("Set HB_URL and HB_KEY (the owner debug-console base URL + capability token).")
        return 2
    cases = load_wiki_lint_cases()
    completer = _GatewayCompleter(base_url, key, task)
    print(f"Calibrating {len(cases)} wiki_lint cases via {base_url} (task route: {task})\n")
    try:
        results = await score_wiki_lint_cases(cases, completer)
    finally:
        await completer.aclose()
    if completer.served:
        print(f"Model served: {completer.served}\n")
    ok = report(results)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
