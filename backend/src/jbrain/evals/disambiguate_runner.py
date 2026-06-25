"""The entity.disambiguate eval scoring core — drives the REAL disambiguation
prompt through a model via the LLM adapter and scores the link decision.

Mirrors `runner.py` (note.extract): `load_disambiguate_cases` reads the corpus
beside this module (`disambiguate_cases/*.json`), `score_disambiguate_cases`
drives each case through an injected router (the adapter — never a provider SDK)
and scores it, and `eval_run_from_disambiguate` adapts the results into a
two-dimensional `{task, safety}` EvalRun.

The decision has an asymmetric cost: a FALSE link (an id chosen when the answer
is "none of these") is the dangerous outcome — it silently fuses two entities —
so it is the SAFETY dimension; a MISSED link (null when an id was right) is the
conservative miss the review inbox catches, scored only against task. So a prompt
edit can never trade a false link for accuracy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jbrain.analysis.entities import (
    DISAMBIGUATE_MAX_TOKENS,
    DISAMBIGUATE_SCHEMA,
    DISAMBIGUATE_STRENGTH,
    DISAMBIGUATE_SYSTEM,
    build_disambiguation_prompt,
    parse_disambiguation,
)
from jbrain.evals.scores import EvalRun, FixtureScore

CASES_DIR = Path(__file__).parent / "disambiguate_cases"

DISAMBIGUATE_TASK = "entity.disambiguate"


def load_disambiguate_cases() -> list[dict[str, Any]]:
    """Every case across disambiguate_cases/*.json (sorted for stable order)."""
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text()))
    return cases


@dataclass
class DisambiguateResult:
    name: str
    # (label, ok, detail): "link" is the task check, "no_false_link" the safety guard.
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    got: str | None = None
    gold: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(ok for _, ok, _ in self.checks)


def _score(case: dict[str, Any], choices: dict[str, str | None]) -> DisambiguateResult:
    name = case["mention"]
    gold = case.get("gold")
    raw = choices.get(name, "∅")  # ∅ = the model never answered this mention
    # The DECISION is "link to this id" or "do not link". Not linking is expressed
    # two equivalent ways — an explicit null choice OR an omitted mention (the
    # pipeline routes both to a new provisional entity) — so both normalize to None.
    decided: str | None = raw if isinstance(raw, str) and raw != "∅" else None
    correct = decided == gold
    # A FALSE link: the answer was "none of these" (gold null) but the model chose a
    # real id — the entity-fusing mistake the safety dimension exists to catch. An
    # omission is never a false link.
    false_link = gold is None and decided is not None
    r = DisambiguateResult(name=case["name"], got=decided, gold=gold)
    detail = f"got {raw!r}"
    r.checks.append((f"link:{case['name']}={gold}", correct, detail))
    r.checks.append((f"no_false_link:{case['name']}", not false_link, detail))
    return r


async def score_disambiguate_cases(
    router: Any, cases: list[dict[str, Any]], *, echo: bool = False
) -> tuple[list[DisambiguateResult], int]:
    """Drive every case through `router` (the adapter), score the link decision,
    and return the results plus total tokens billed. Router-injectable so CI can
    pass a faked router and the box calibration driver passes a live one."""
    results: list[DisambiguateResult] = []
    tokens = 0
    for case in cases:
        item = {
            "name": case["mention"],
            "kind": case.get("kind", "Thing"),
            "context": case.get("context", ""),
            "candidates": case["candidates"],
        }
        try:
            out = await router.complete(
                DISAMBIGUATE_TASK,
                system=DISAMBIGUATE_SYSTEM,
                user_text=build_disambiguation_prompt([item]),
                json_schema=DISAMBIGUATE_SCHEMA,
                max_tokens=DISAMBIGUATE_MAX_TOKENS,
                strength=DISAMBIGUATE_STRENGTH,
            )
            tokens += out.usage.input_tokens + out.usage.output_tokens
            r = _score(case, parse_disambiguation(out.parsed))
        except Exception as exc:  # a live call fails many ways; report, don't crash the run
            r = DisambiguateResult(name=case["name"], error=f"{type(exc).__name__}: {exc}")
        if echo:
            print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name}", flush=True)
        results.append(r)
    return results, tokens


def eval_run_from_disambiguate(results: list[DisambiguateResult], version: str) -> EvalRun:
    """Adapt to the promotion gate's EvalRun: task = correct link decision;
    safety = the no-false-link guard. So a candidate prompt must keep its link
    accuracy AND never start fusing entities to be promoted."""
    scores: list[FixtureScore] = []
    for r in results:
        if r.error:
            scores.append(FixtureScore(r.name, 0.0, 0.0))
            continue
        task = float(all(ok for label, ok, _ in r.checks if label.startswith("link:")))
        guards = [ok for label, ok, _ in r.checks if label.startswith("no_false_link:")]
        safety = (sum(guards) / len(guards)) if guards else 1.0
        scores.append(FixtureScore(r.name, task, safety))
    return EvalRun(version, tuple(scores))
