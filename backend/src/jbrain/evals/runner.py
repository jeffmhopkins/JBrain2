"""The note.extract eval scoring core — runs the REAL system prompt through a
REAL model via the LLM adapter and scores the model's own output.

`load_cases` reads the curated corpus shipped beside this module
(`cases/*.json`), `score_cases` drives each case's note through an injected
router (the adapter — never a provider SDK) and scores it, and
`eval_run_from_cases` adapts the results into a two-dimensional
`{task, safety}` `EvalRun`. It lives in the `jbrain` package (not the
dev-only `backend/evals/` CLI) so it ships in the container image and the nightly
schedule can score the live model in production.

The dev CLI (`backend/evals/run.py`) and the offline audit (`backend/evals/audit.py`)
import their building blocks from here; the CLI keeps only its argparse/report glue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jbrain.analysis.extraction import parse_extraction
from jbrain.analysis.prompt import (
    EXTRACT_MAX_TOKENS,
    EXTRACTION_SCHEMA,
    NOTE_EXTRACT_STRENGTH,
    SYSTEM_PROMPT,
    build_user_prompt,
    fact_cap,
)
from jbrain.evals.scores import EvalRun, FixtureScore

CASES_DIR = Path(__file__).parent / "cases"


def load_cases() -> list[dict[str, Any]]:
    """Every case across all cases/*.json — agents drop in their own file and it's
    picked up automatically (sorted for a stable run order)."""
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text()))
    return cases


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _overlaps(a: str, b: str) -> bool:
    """One name contains the other AS WHOLE WORDS ('Celine' ~ 'Celine
    Hopkins') — the eval tolerates first-name-vs-full-name without rewarding a
    wholly wrong name, and without a short token matching INSIDE a longer word
    (raw substring let 'Me' match the 'me' in 'Chase Home Lending')."""
    ta, tb = re.findall(r"[0-9a-z]+", a.lower()), re.findall(r"[0-9a-z]+", b.lower())
    if not ta or not tb:
        return False
    short, long = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return any(long[i : i + len(short)] == short for i in range(len(long) - len(short) + 1))


def _pred_norm(p: str) -> str:
    """Predicate spellings compared style-blind: 'personalBest', 'personal_best',
    and 'personal-best' are one predicate. Whole-token overlap can't see this
    ('personalBest' is one token, 'personal_best' is two), which would let a
    salience negative pass silently on a snake_case spelling."""
    return re.sub(r"[_\-]", "", p).casefold()


@dataclass
class CaseResult:
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (label, ok, detail)
    dump: str = ""  # compact rendering of what the model returned, for diagnosis
    error: str | None = None
    # Facts the model emitted for this case — summed across the corpus this is
    # the "leaner" metric a salience-first prompt change is measured by
    # (docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md §7).
    fact_count: int = 0

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
    domains = [f"{f.predicate}:{f.domain or '-'}" for f in parsed.facts]
    lines = [f"      mentions: {mentions}"]
    if edges:
        lines.append(f"      edges: {', '.join(edges)}")
    if valued:
        lines.append(f"      facts: {', '.join(valued)}")
    if domains:
        lines.append(f"      domains: {', '.join(domains)}")
    if temporal:
        lines.append(f"      dates: {', '.join(temporal)}")
    return "\n".join(lines)


def _score(case: dict[str, Any], parsed: Any, anchor: datetime) -> CaseResult:
    res = CaseResult(name=case["name"], dump=_dump(parsed, anchor), fact_count=len(parsed.facts))
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

    # The model assigned at least one fact to this domain — measures per-fact
    # domain classification (the LLM's judgment; the deterministic type->domain
    # floor, when it lands, would make this robust regardless of the model).
    for dom in expect.get("domain", []):
        res.checks.append((f"domain:{dom}", any(f.domain == dom for f in parsed.facts), ""))

    # Salience negatives (the salience-first contract: mentions stay generous,
    # long-tail facts stay in the prose). `absent_edges` — no fact may LINK
    # this object into the graph, whether as the fact's SUBJECT (entity_ref)
    # or its object (a mention is fine); `absent_predicates` — no fact may
    # carry this predicate, compared style-blind so a snake_case respelling
    # can't evade it. Labels deliberately do NOT use the "absent:"
    # groundedness prefix: a salience miss is a task loss, not a fabrication.
    for spec in expect.get("absent_edges", []):
        obj = spec["object"]
        linked = any(
            _overlaps(obj, f.entity_ref)
            or (f.object_entity_ref and _overlaps(obj, f.object_entity_ref))
            for f in parsed.facts
        )
        res.checks.append((f"absent_edge->{obj}", not linked, ""))
    for pred in expect.get("absent_predicates", []):
        present = any(_pred_norm(pred) in _pred_norm(f.predicate) for f in parsed.facts)
        res.checks.append((f"absent_predicate:{pred}", not present, ""))

    return res


def _print_case(r: CaseResult) -> None:
    # Stream each case as it finishes (flush, since output is usually piped) so a
    # long run shows progress and a timeout still yields the cases that ran.
    print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name}", flush=True)
    if r.error:
        print(f"      ERROR {r.error}", flush=True)
        return
    for label, ok, _ in r.checks:
        if not ok:
            print(f"      miss {label}", flush=True)
    if not r.passed and r.dump:
        print(r.dump, flush=True)  # what the model returned, so the prompt can be tuned


async def score_cases(
    router: Any, cases: list[dict[str, Any]], *, echo: bool = False
) -> tuple[list[CaseResult], int]:
    """Drive every case's note through `router` (the LLM adapter — never a provider
    SDK), score the model's own output, and return the results plus the total tokens
    billed across the run. Router-injectable so callers can run it against the live
    adapter or a faked router (CI passes a faked router), and the returned token total
    lets a caller account the spend.

    Parses WITH the case anchor, exactly as the pipeline does for a note whose client
    offset is known: the score reflects what the app actually STORES (model output
    plus the deterministic backward-date repair), so a green eval means a green app,
    not just a green prompt. `echo` streams each case (the CLI report); the action
    leaves it off."""
    results: list[CaseResult] = []
    tokens = 0
    for case in cases:
        anchor = datetime.fromisoformat(case["created_at"])
        cap = fact_cap(case["body"])
        user = build_user_prompt(
            [case["body"]], anchor=anchor, domain=case.get("domain", "general"), max_facts=cap
        )
        try:
            out = await router.complete(
                "note.extract",
                system=SYSTEM_PROMPT,
                user_text=user,
                json_schema=EXTRACTION_SCHEMA,
                max_tokens=EXTRACT_MAX_TOKENS,
                strength=NOTE_EXTRACT_STRENGTH,
            )
            tokens += out.usage.input_tokens + out.usage.output_tokens
            r = _score(case, parse_extraction(out.parsed, anchor=anchor, max_facts=cap), anchor)
        except Exception as exc:  # a live call can fail many ways; report, don't crash the run
            r = CaseResult(name=case["name"], error=f"{type(exc).__name__}: {exc}")
        if echo:
            _print_case(r)
        results.append(r)
    return results, tokens


# Check-label prefixes that guard against fabricated/over-personified entities —
# the groundedness dimension scored separately from task success, so the two
# dimensions stay distinguishable (task points can't mask a groundedness loss).
_GROUNDEDNESS_PREFIXES = ("absent:", "not_person:")


def eval_run_from_cases(results: list[CaseResult], version: str) -> EvalRun:
    """Adapt the note.extract eval's CaseResults into an EvalRun: task = fraction of
    checks passed; safety = fraction of the groundedness-guard checks passed (1.0 when
    a case has none). Keeping the two dimensions split is what lets a reader see a
    task win that came at the cost of groundedness on the existing set."""
    scores: list[FixtureScore] = []
    for r in results:
        if r.error:
            scores.append(FixtureScore(r.name, 0.0, 0.0))
            continue
        if not r.checks:
            scores.append(FixtureScore(r.name, 1.0, 1.0))
            continue
        task = sum(ok for _, ok, _ in r.checks) / len(r.checks)
        guards = [ok for label, ok, _ in r.checks if label.startswith(_GROUNDEDNESS_PREFIXES)]
        safety = (sum(guards) / len(guards)) if guards else 1.0
        scores.append(FixtureScore(r.name, task, safety))
    return EvalRun(version, tuple(scores))
