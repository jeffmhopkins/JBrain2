"""LLM-in-the-loop eval for the note.extract prompt (opt-in, NOT run in CI).

Unlike the deterministic harness (tests/harness — which scripts a perfect model
and exercises the pipeline), this runs the REAL system prompt through a REAL
model via the LLM adapter and scores the model's own output. It is how a prompt
change (e.g. note-extract-v5's object-person + backward-temporal guidance) is
MEASURED rather than guessed — the gap the harness explicitly cannot cover
("does not test the prompt — only a live model exercises that").

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
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jbrain.analysis.extraction import parse_extraction
from jbrain.analysis.pipeline import EXTRACT_MAX_TOKENS
from jbrain.analysis.prompt import (
    EXTRACTION_SCHEMA,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from jbrain.config import Settings
from jbrain.llm import build_router

CASES_DIR = Path(__file__).parent / "cases"


def load_cases() -> list[dict[str, Any]]:
    """Every case across all evals/cases/*.json — agents drop in their own file
    and it's picked up automatically (sorted for a stable run order)."""
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text()))
    return cases


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
    dump: str = ""  # compact rendering of what the model returned, for diagnosis
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(ok for _, ok, _ in self.checks)


def _local_date(dt: datetime, anchor: datetime) -> str:
    """The calendar date the instant falls on in the note's LOCAL timezone —
    what the app shows. An absolute date stored as midnight-UTC is the prior
    local day at a western offset, so a raw UTC .date() would mis-read it."""
    return dt.astimezone(anchor.tzinfo).date().isoformat()


def _dump(parsed: Any, anchor: datetime) -> str:
    """What the model actually returned, compact enough to paste — the thing I
    read to iterate the prompt when a case fails."""
    mentions = ", ".join(f"{m.name}:{m.kind}" for m in parsed.mentions) or "(none)"
    edges = [
        f"{f.entity_ref}.{f.predicate}->{f.object_entity_ref}"
        for f in parsed.facts
        if f.object_entity_ref
    ]
    temporal = [
        f"{f.temporal.phrase!r}={_local_date(f.temporal.resolved_start, anchor)}"
        for f in parsed.facts
        if f.temporal and f.temporal.phrase and f.temporal.resolved_start
    ] + [
        f"{t.phrase!r}={_local_date(t.resolved_start, anchor)}"
        for t in parsed.tokens
        if t.phrase and t.resolved_start
    ]
    valued = [f"{f.predicate}={json.dumps(f.value_json)}" for f in parsed.facts if f.value_json]
    lines = [f"      mentions: {mentions}"]
    if edges:
        lines.append(f"      edges: {', '.join(edges)}")
    if valued:
        lines.append(f"      facts: {', '.join(valued)}")
    if temporal:
        lines.append(f"      dates: {', '.join(temporal)}")
    return "\n".join(lines)


def _score(case: dict[str, Any], parsed: Any, anchor: datetime) -> CaseResult:
    res = CaseResult(name=case["name"], dump=_dump(parsed, anchor))
    expect = case.get("expect", {})
    mention_names = [m.name for m in parsed.mentions]

    for person in expect.get("person_mentions", []):
        hit = next((n for n in mention_names if _overlaps(person, n)), None)
        res.checks.append((f"person:{person}", hit is not None, ""))

    # Presence of any-kind entity (org, group, place, concrete concept) by name.
    for name in expect.get("mentions", []):
        hit = any(_overlaps(name, n) for n in mention_names)
        res.checks.append((f"mention:{name}", hit, ""))

    # Present AND typed within an allowed kind family (case-insensitive) — a
    # generous set per case, since models name kinds variably (Organization vs
    # Corporation, Place vs City).
    for spec in expect.get("mention_kind", []):
        allowed = {k.lower() for k in spec["kind"]}
        ok = any(
            _overlaps(spec["name"], m.name) and m.kind.lower() in allowed for m in parsed.mentions
        )
        res.checks.append((f"kind:{spec['name']}", ok, ""))

    # Negative check: a name the model must NOT promote to a mention AT ALL —
    # for fabricated humans / pure non-entities ("someone", a guessed name).
    for person in expect.get("absent_person", []):
        present = any(_overlaps(person, n) for n in mention_names)
        res.checks.append((f"absent:{person}", not present, ""))

    # Over-personification check: a token that may legitimately be a non-Person
    # mention (a Product, Place, Animal, CreativeWork) but must NOT be typed as a
    # Person. Passes if it's absent or present with a non-Person kind.
    for name in expect.get("not_person", []):
        mis = any(_overlaps(name, m.name) and m.kind.lower() == "person" for m in parsed.mentions)
        res.checks.append((f"not_person:{name}", not mis, ""))

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
        res.checks.append((f"edge->{obj}", match is not None, ""))

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
        # Compare in the note's LOCAL tz, exactly as the app renders the date.
        got = {_local_date(s, anchor) for s in starts if s is not None}
        res.checks.append((f"temporal:{phrase}={want}", want in got, ""))

    # A fact carries a value (a measurement/amount): some fact's rendered
    # value_json + statement contains the wanted text, optionally on a predicate
    # matching `predicate` (substring-overlap). For weight/BP/money extraction.
    for v in expect.get("value", []):
        want = str(v["contains"]).casefold()
        pred = v.get("predicate")
        hit = any(
            (pred is None or _overlaps(pred, f.predicate))
            and want in f"{json.dumps(f.value_json or {})} {f.statement}".casefold()
            for f in parsed.facts
        )
        res.checks.append((f"value:{v.get('predicate', '')}~{v['contains']}", hit, ""))

    return res


async def _run(cases: list[dict[str, Any]]) -> tuple[list[CaseResult], str]:
    # Parse WITH the anchor, exactly as the pipeline does for a note whose
    # client offset is known: the score then reflects what the app actually
    # STORES — model output plus the deterministic backward-date repair — so a
    # green eval means a green app, not just a green prompt.
    router = build_router(Settings())
    provider, model = router.spec("note.extract")
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
            results.append(_score(case, parse_extraction(out.parsed, anchor=anchor), anchor))
        except Exception as exc:  # a live call can fail many ways; report, don't crash the run
            results.append(CaseResult(name=case["name"], error=f"{type(exc).__name__}: {exc}"))
    return results, f"{provider}:{model}"


def _report(results: list[CaseResult], model: str) -> bool:
    print(
        f"prompt-eval — {model} — {PROMPT_VERSION} — {datetime.now().isoformat(timespec='seconds')}"
    )
    print("-" * 64)
    checks_total = checks_ok = 0
    for r in results:
        print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name}")
        if r.error:
            print(f"      ERROR {r.error}")
            continue
        for label, ok, _ in r.checks:
            checks_total += 1
            checks_ok += ok
            if not ok:
                print(f"      miss {label}")
        if not r.passed and r.dump:
            print(r.dump)  # show what the model returned so the prompt can be tuned
    passed = sum(r.passed for r in results)
    pct = (100 * checks_ok / checks_total) if checks_total else 0.0
    print("-" * 64)
    print(f"{passed}/{len(results)} cases passed; {checks_ok}/{checks_total} checks ({pct:.0f}%)")
    return passed == len(results)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", help="run only the named case")
    ap.add_argument("--like", help="run only cases whose name contains any of these (comma-separated)")
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
    results, model = asyncio.run(_run(cases))
    all_passed = _report(results, model)
    return 0 if (all_passed or not args.strict) else 1


if __name__ == "__main__":
    raise SystemExit(main())
