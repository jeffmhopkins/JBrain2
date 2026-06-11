"""LLM-in-the-loop eval for the note.extract prompt (opt-in, NOT run in CI).

Unlike the deterministic harness (tests/harness — which scripts a perfect model
and exercises the pipeline), this runs the REAL system prompt through a REAL
model via the LLM adapter and scores the model's own output. It is how a prompt
change (e.g. note-extract-v5's object-person + backward-temporal guidance) is
MEASURED rather than guessed — the gap the harness explicitly cannot cover
("does not test the prompt — only a live model exercises that").

Run it against whatever provider/model your config points note.extract at
(JBRAIN_LLM_TASKS, provider keys / base URLs). It is provider-agnostic, so the
same cases score Claude, grok, or a local model. CI never calls a live model,
so this lives outside the test suite and is invoked by hand:

    cd backend && uv run python -m evals.run            # all cases
    uv run python -m evals.run --strict                 # exit 1 if any case fails
    uv run python -m evals.run --case marriage_copular_object
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jbrain.analysis.extraction import parse_extraction
from jbrain.analysis.pipeline import EXTRACT_MAX_TOKENS
from jbrain.analysis.prompt import EXTRACTION_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from jbrain.config import Settings
from jbrain.llm import build_router

CASES_FILE = Path(__file__).parent / "cases.json"


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _overlaps(a: str, b: str) -> bool:
    """One name contains the other ('Celine' ~ 'Celine Hopkins') — the eval
    tolerates first-name-vs-full-name without rewarding a wholly wrong name."""
    na, nb = _norm(a), _norm(b)
    return bool(na) and bool(nb) and (na in nb or nb in na)


@dataclass
class CaseResult:
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (label, ok, detail)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(ok for _, ok, _ in self.checks)


def _score(case: dict[str, Any], parsed: Any) -> CaseResult:
    res = CaseResult(name=case["name"])
    expect = case.get("expect", {})
    mention_names = [m.name for m in parsed.mentions]

    for person in expect.get("person_mentions", []):
        hit = next((n for n in mention_names if _overlaps(person, n)), None)
        res.checks.append((f"person:{person}", hit is not None, f"got {mention_names}"))

    for edge in expect.get("edges", []):
        obj = edge["object"]
        match = next(
            (
                f
                for f in parsed.facts
                if f.object_entity_ref and _overlaps(obj, f.object_entity_ref)
            ),
            None,
        )
        detail = (
            f"{match.predicate} -> {match.object_entity_ref}"
            if match
            else f"no edge with object ~ {obj!r}"
        )
        res.checks.append((f"edge->{obj}", match is not None, detail))

    for t in expect.get("temporal", []):
        phrase, want = t["phrase"], t["resolved_date"]
        starts = [
            f.temporal.resolved_start
            for f in parsed.facts
            if f.temporal and f.temporal.phrase and _overlaps(phrase, f.temporal.phrase)
        ] + [
            tok.resolved_start
            for tok in parsed.tokens
            if tok.phrase and _overlaps(phrase, tok.phrase)
        ]
        got = {s.date().isoformat() for s in starts if s is not None}
        res.checks.append((f"temporal:{phrase}={want}", want in got, f"got {got or 'none'}"))

    return res


async def _run(cases: list[dict[str, Any]]) -> list[CaseResult]:
    # Parse WITHOUT an anchor: the eval measures the MODEL's own resolution, not
    # the deterministic backward-date repair (that is unit-tested separately).
    router = build_router(Settings())
    results: list[CaseResult] = []
    for case in cases:
        anchor = datetime.fromisoformat(case["created_at"])
        user = build_user_prompt(
            [case["body"]], anchor=anchor, domain=case.get("domain", "general")
        )
        try:
            out = await router.complete(
                "note.extract",
                system=SYSTEM_PROMPT,
                user_text=user,
                json_schema=EXTRACTION_SCHEMA,
                max_tokens=EXTRACT_MAX_TOKENS,
            )
            results.append(_score(case, parse_extraction(out.parsed)))
        except Exception as exc:  # a live call can fail many ways; report, don't crash the run
            results.append(CaseResult(name=case["name"], error=f"{type(exc).__name__}: {exc}"))
    return results


def _report(results: list[CaseResult]) -> int:
    checks_total = checks_ok = 0
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.name}")
        if r.error:
            print(f"    ERROR {r.error}")
        for label, ok, detail in r.checks:
            checks_total += 1
            checks_ok += ok
            if not ok:
                print(f"    miss {label}  ({detail})")
    passed = sum(r.passed for r in results)
    pct = (100 * checks_ok / checks_total) if checks_total else 0.0
    print(f"\n{passed}/{len(results)} cases passed; {checks_ok}/{checks_total} checks ({pct:.0f}%)")
    return passed == len(results)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", help="run only the named case")
    ap.add_argument("--strict", action="store_true", help="exit 1 unless every case passes")
    args = ap.parse_args()

    cases = json.loads(CASES_FILE.read_text())
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
        if not cases:
            print(f"no case named {args.case!r}", file=sys.stderr)
            return 2
    all_passed = _report(asyncio.run(_run(cases)))
    return 0 if (all_passed or not args.strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
