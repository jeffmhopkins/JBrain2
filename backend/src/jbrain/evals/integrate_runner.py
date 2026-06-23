"""The integrate.note eval scoring core — drives the REAL integrate prompt
through a model via the LLM adapter and scores the IntegrationIntent's judgment.

Mirrors `runner.py` (note.extract): `load_integrate_cases` reads the corpus
beside this module (`integrate_cases/*.json`), `score_integrate_cases` builds the
deterministic INPUT each case describes — an `Extraction` (mentions + candidate
facts) plus a rendered `graph_context` (existing entities + facts) — drives it
through an injected router (the adapter — never a provider SDK), parses the reply
with `parse_intent`, and scores it against per-case judgment golds.

A case asserts the judgment the integrator must get right: resolve the owner /
namesakes, propose supersede vs accumulate vs conflict per fact kind, flag
cross-subject and ambiguous, and never mint an entity from a name. The SAFETY
dimension is the data-integrity guards (never mint a name, never put a sentence
in value_json); task is the rest. So a prompt edit can never trade an entity
fabrication or a prose value for judgment points.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jbrain.analysis.extraction import ExtractedFact, ExtractedMention, Extraction
from jbrain.analysis.graph_context import (
    CandidateEntity,
    FactLine,
    rank_and_bound,
    render_graph_context,
)
from jbrain.analysis.integrate_prompt import (
    INTEGRATE_MAX_TOKENS,
    INTEGRATE_STRENGTH,
    INTEGRATE_SYSTEM,
    build_integrate_prompt,
)
from jbrain.analysis.intent import IntegrationIntent
from jbrain.analysis.intent_parse import INTENT_SCHEMA, parse_intent
from jbrain.workflow.promotion import EvalRun, FixtureScore

CASES_DIR = Path(__file__).parent / "integrate_cases"

INTEGRATE_TASK = "integrate.note"
# A value is a bare datum; a string leaf this many words long is prose — a
# sentence stuffed into value_json, which the integrate prompt forbids. A length
# proxy (not a parser) keeps the check robust: a real datum ("Dr. Patel", "oat
# milk", "128/82 mmHg") is short, a sentence is not.
_MAX_DATUM_WORDS = 10


def load_integrate_cases() -> list[dict[str, Any]]:
    """Every case across integrate_cases/*.json (sorted for stable order)."""
    cases: list[dict[str, Any]] = []
    for path in sorted(CASES_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text()))
    return cases


@dataclass
class IntegrateResult:
    name: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (label, ok, detail)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(ok for _, ok, _ in self.checks)


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _fact_lines(facts: list[dict[str, Any]]) -> tuple[FactLine, ...]:
    return tuple(
        FactLine(
            predicate=f["predicate"], qualifier=f.get("qualifier", ""), kind=f["kind"],
            assertion=f["assertion"], value=f.get("value", ""),
            valid_from=_dt(f.get("valid_from")), valid_to=_dt(f.get("valid_to")),
        )
        for f in facts
    )


def _candidate(d: dict[str, Any]) -> CandidateEntity:
    return CandidateEntity(
        entity_id=d["id"], name=d["name"], kind=d["kind"],
        aliases=tuple(d.get("aliases", ())), facts=_fact_lines(d.get("facts", [])),
    )


def build_inputs(case: dict[str, Any]) -> tuple[Extraction, str]:
    """The deterministic integrate INPUT a case describes: an Extraction and the
    rendered graph_context. Pure — no DB, no model — so the case fixes everything
    except the model's judgment."""
    mentions = [
        ExtractedMention(name=m["name"], kind=m["kind"], surface_text=m["surface"])
        for m in case["mentions"]
    ]
    facts = [
        ExtractedFact(
            predicate=f["predicate"], qualifier=f.get("qualifier", ""), kind=f["kind"],
            statement=f["statement"], value_json=f.get("value_json"), assertion=f["assertion"],
            entity_ref=f["entity_ref"], object_entity_ref=f.get("object_entity_ref"),
            temporal=None, domain=f.get("domain", "general"), confidence=0.9,
        )
        for f in case["facts"]
    ]
    extraction = Extraction(
        title=case["name"][:60], tags=["calib"], mentions=mentions, facts=facts, tokens=[]
    )
    owner = _candidate(case["owner"])
    others = [_candidate(o) for o in case.get("others", [])]
    ctx = render_graph_context(rank_and_bound(owner, others))
    return extraction, ctx


def _score(case: dict[str, Any], intent: IntegrationIntent) -> IntegrateResult:
    g = case["gold"]
    r = IntegrateResult(name=case["name"])
    res = {x.mention_ref: x for x in intent.entity_resolutions}
    sup = {s.predicate: s.action for s in intent.supersession_proposals}

    for mref, eid in g.get("resolve_existing", {}).items():
        x = res.get(mref)
        ok = bool(x and x.mode == "existing" and x.proposed_entity_id == eid)
        r.checks.append((f"resolve:{mref}={eid}", ok, f"{x.mode if x else 'none'}"))
    for pred, act in g.get("supersede", {}).items():
        r.checks.append((f"supersede:{pred}={act}", sup.get(pred) == act, f"{sup.get(pred)}"))
    for pred in g.get("no_supersede", []):
        r.checks.append((f"no_supersede:{pred}", sup.get(pred) != "supersede", f"{sup.get(pred)}"))
    for pred in g.get("conflict", []):
        r.checks.append((f"conflict:{pred}", sup.get(pred) == "conflict", f"{sup.get(pred)}"))
    for mref, want in g.get("cross_subject", {}).items():
        x = res.get(mref)
        r.checks.append((f"cross_subject:{mref}={want}", bool(x and x.cross_subject == want), ""))
    for mref in g.get("cross_subject_false", []):
        x = res.get(mref)
        r.checks.append((f"cross_subject:{mref}=False", not (x and x.cross_subject), ""))
    for mref in g.get("ambiguous", []):
        x = res.get(mref)
        r.checks.append((f"ambiguous:{mref}", bool(x and x.mode == "ambiguous"), ""))
    # SAFETY guard 1 — never mint an entity from a name/nickname/alias value.
    for nm in g.get("no_mint_name", []):
        minted = any(
            x.mode == "new" and nm.lower() in (x.new_name or "").lower()
            for x in intent.entity_resolutions
        )
        r.checks.append((f"no_mint_name:{nm}", not minted, ""))
    # SAFETY guard 2 — never carry a sentence into value_json (length proxy).
    prose = next(
        (
            f"{f.predicate}={v!r}"
            for f in intent.facts
            if isinstance(f.value_json, dict)
            for v in f.value_json.values()
            if isinstance(v, str) and len(v.split()) > _MAX_DATUM_WORDS
        ),
        None,
    )
    r.checks.append((f"no_value_sentence:{case['name']}", prose is None, prose or ""))
    return r


async def score_integrate_cases(
    router: Any, cases: list[dict[str, Any]], *, echo: bool = False
) -> tuple[list[IntegrateResult], int]:
    """Drive every case through `router` (the adapter), score the IntegrationIntent
    against its golds, and return the results plus total tokens billed. Router-
    injectable so CI passes a faked router and the box driver passes a live one."""
    results: list[IntegrateResult] = []
    tokens = 0
    for case in cases:
        try:
            extraction, ctx = build_inputs(case)
            out = await router.complete(
                INTEGRATE_TASK,
                system=INTEGRATE_SYSTEM,
                user_text=build_integrate_prompt(extraction, ctx, case.get("note_text", "")),
                json_schema=INTENT_SCHEMA,
                max_tokens=INTEGRATE_MAX_TOKENS,
                strength=INTEGRATE_STRENGTH,
            )
            tokens += out.usage.input_tokens + out.usage.output_tokens
            intent = parse_intent(
                out.parsed, note_id=case["name"], schema_version=1,
                prompt_version="eval", integrator_version="eval",
            )
            r = _score(case, intent)
        except Exception as exc:  # a live call / parse fails many ways; report, don't crash
            r = IntegrateResult(name=case["name"], error=f"{type(exc).__name__}: {exc}")
        if echo:
            print(f"[{'PASS' if r.passed else 'FAIL'}] {r.name}", flush=True)
        results.append(r)
    return results, tokens


# Data-integrity guards scored separately from judgment: a prompt edit can't
# trade an entity fabrication or a prose value for judgment points.
_SAFETY_PREFIXES = ("no_mint_name:", "no_value_sentence:")


def eval_run_from_integrate(results: list[IntegrateResult], version: str) -> EvalRun:
    """Adapt to the promotion gate's EvalRun: task = fraction of judgment checks
    passed; safety = fraction of the data-integrity guards passed (1.0 when none)."""
    scores: list[FixtureScore] = []
    for r in results:
        if r.error:
            scores.append(FixtureScore(r.name, 0.0, 0.0))
            continue
        if not r.checks:
            scores.append(FixtureScore(r.name, 1.0, 1.0))
            continue
        task = sum(ok for _, ok, _ in r.checks) / len(r.checks)
        guards = [ok for label, ok, _ in r.checks if label.startswith(_SAFETY_PREFIXES)]
        safety = (sum(guards) / len(guards)) if guards else 1.0
        scores.append(FixtureScore(r.name, task, safety))
    return EvalRun(version, tuple(scores))
