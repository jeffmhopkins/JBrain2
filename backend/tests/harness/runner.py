"""Run a Scenario against a real Postgres through the genuine integrate_note
pipeline, then snapshot the graph for the checker.

We are BOTH models: each step scripts the note.extract response (the
extraction) and the integrate.note response (the Integrator's intent). The
intent is compiled from the step — explicit when the scenario authored one,
else a faithful default (name-match resolution against the live graph, every
surface-attested fact committed) — with existing-entity references resolved to
their live ids at step time, the way the real agent reads them from graph
context. Everything downstream (canonicalize → plan_intent → apply_intent →
the arbiter's supersession/inverse/review writes) is the real pipeline.

Usable two ways:
  - pytest (tests/integration/test_harness_scenarios.py) drives run_scenario
    against the shared testcontainers database fixture.
  - CLI for interactive "be the model" work against a standing DB (see
    scripts/llm-harness.sh):
      python -m tests.harness.runner prompt    # print the real assembled prompt
      python -m tests.harness.runner run FILE   # run one scenario, print result
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.analysis.entities import get_or_create_me
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.queue import SYSTEM_CTX
from tests.harness.scenario import (
    EntityRow,
    FactRow,
    ReviewRow,
    Scenario,
    Snapshot,
    Step,
    check,
    load_scenario,
)


def _integrator(
    maker: async_sessionmaker, extraction_json: str, intent_json: str
) -> AnalysisPipeline:
    """A pipeline whose two model calls return exactly this step's scripted JSON
    (we are both models); routing/tasks mirror the real default."""
    router = LlmRouter(
        {"xai": FakeLlmClient([extraction_json, intent_json])},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )
    return AnalysisPipeline(maker, router)


async def _entity_id_by_name(maker: async_sessionmaker, name: str, domain: str) -> str | None:
    """The live id of the most recent non-retracted entity with this canonical
    name — how the runner, acting as the agent, resolves an existing-mode
    reference (and merge/distinct pairs) to a real id at step time. 'Me' is the
    owner, which lives in the general domain; everyone else is matched within the
    note's own domain (the firewall the real resolver respects)."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(
                text(
                    "SELECT id::text FROM app.entities"
                    " WHERE canonical_name = :n AND status <> 'retracted'"
                    "   AND (:d = 'general' OR domain_code = :d OR canonical_name = 'Me')"
                    " ORDER BY created_at DESC LIMIT 1"
                ),
                {"n": name, "d": domain},
            )
        ).scalar_one_or_none()


async def _compile_intent(maker: async_sessionmaker, step: Step, domain: str) -> str:
    """Produce the integrate.note JSON for this step.

    Default (no scripted intent): one resolution per referenced name — existing
    when a live entity already carries that canonical name (so name-stable
    dedup/supersession works across steps), else new; one fact per extraction
    fact, surface-attested (so the arbiter commits it) at its subject's surface.

    Explicit intent: passed through, but every existing-mode resolution and
    merge/distinct pair names its entity (`name`/`entity_a`/`entity_b`); the
    runner swaps each for its live id here so authors never hard-code uuids."""
    extraction = step.extraction
    # Ensure the owner exists before we resolve "Me" to it.
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await get_or_create_me(s)

    if step.intent is not None:
        return await _compile_explicit_intent(maker, step.intent, domain)

    mentions = extraction.get("mentions", [])
    kind_by_name = {m["name"]: m.get("kind", "Thing") for m in mentions}
    surface_by_name = {m["name"]: m.get("surface_text", m["name"]) for m in mentions}
    body_surface = next(iter(surface_by_name.values()), step.body[:24])

    # Default: resolve every referenced name, commit every fact.
    refs: list[str] = []
    for m in extraction.get("mentions", []):
        if m["name"] not in refs:
            refs.append(m["name"])
    for f in extraction.get("facts", []):
        for ref in (f.get("entity_ref"), f.get("object_entity_ref")):
            if ref and ref not in refs:
                refs.append(ref)

    resolutions = []
    for name in refs:
        existing = await _entity_id_by_name(maker, name, domain)
        if existing is not None:
            resolutions.append({"mention_ref": name, "mode": "existing", "entity_id": existing})
        else:
            resolutions.append(
                {
                    "mention_ref": name,
                    "mode": "new",
                    "new_kind": kind_by_name.get(name, "Thing"),
                    "new_name": name,
                }
            )

    facts = []
    for f in extraction.get("facts", []):
        surface = surface_by_name.get(f.get("entity_ref"), body_surface)
        facts.append(
            {
                "entity_ref": f["entity_ref"],
                "predicate": f["predicate"],
                "qualifier": f.get("qualifier", ""),
                "kind": f["kind"],
                "statement": f["statement"],
                "value_json": f.get("value_json"),
                "assertion": f["assertion"],
                "object_entity_ref": f.get("object_entity_ref"),
                "self_confidence": f.get("confidence", 0.9),
                "inferred": False,
                "surface": surface,
                "temporal": f.get("temporal"),
            }
        )
    return json.dumps({"resolutions": resolutions, "facts": facts})


async def _compile_explicit_intent(
    maker: async_sessionmaker, intent: dict[str, Any], domain: str
) -> str:
    """Resolve the name-based references in an authored intent to live ids."""
    out: dict[str, Any] = {"resolutions": [], "facts": list(intent.get("facts", []))}
    for r in intent.get("resolutions", []):
        r = dict(r)
        if r.get("mode") == "existing" and "entity_id" not in r:
            name = r.get("name", r["mention_ref"])
            r["entity_id"] = await _entity_id_by_name(maker, name, domain)
        out["resolutions"].append(r)
    for key in ("supersession_proposals", "merge_proposals", "distinct_proposals"):
        items = intent.get(key)
        if not items:
            continue
        resolved = []
        for p in items:
            p = dict(p)
            for end in ("entity_a", "entity_b"):
                if end in p:
                    p[f"{end}_id"] = await _entity_id_by_name(maker, p.pop(end), domain)
            resolved.append(p)
        out[key] = resolved
    return json.dumps(out)


async def _seed_note(maker: async_sessionmaker, step: Step) -> str:
    """Insert one note + a single chunk (body verbatim) with the step's exact
    created_at — reported_at and the temporal anchor every assertion turns on.
    One chunk keeps span-anchoring deterministic; chunk splitting is covered by
    the ingest tests, not here."""
    note_id = str(uuid.uuid4())
    created = datetime.fromisoformat(step.created_at)
    # Carry the step's local offset like a real capture would: the pipeline's
    # local_anchor (and the backward-phrase repair that rides it) needs it, and
    # without it an evening-capture scenario would resolve against the UTC day.
    offset = created.utcoffset()
    tz_offset = int(offset.total_seconds() // 60) if offset is not None else None
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at,"
                " tz_offset_minutes) VALUES (:i, :c, :d, :b, :t, :tz)"
            ),
            {
                "i": note_id,
                "c": note_id[:12],
                "d": step.domain,
                "b": step.body,
                "t": created,
                "tz": tz_offset,
            },
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i, :n, :d, 'paragraph', 1, :b)"
            ),
            {"i": str(uuid.uuid4()), "n": note_id, "d": step.domain, "b": step.body},
        )
        await s.commit()
    return note_id


async def _snapshot(maker: async_sessionmaker) -> Snapshot:
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        facts = (
            await s.execute(
                text(
                    "SELECT e.canonical_name AS entity, f.predicate, f.qualifier, f.kind,"
                    " f.assertion, f.status, f.statement, f.value_json,"
                    " f.superseded_by IS NOT NULL AS chained, f.pinned, f.domain_code AS domain"
                    " FROM app.facts f JOIN app.entities e ON e.id = f.entity_id"
                )
            )
        ).all()
        reviews = (
            await s.execute(
                text(
                    "SELECT kind, coalesce(payload->>'summary','') AS summary, status,"
                    " domain_code AS domain FROM app.review_items"
                )
            )
        ).all()
        entities = (
            await s.execute(text("SELECT canonical_name AS name, kind, status FROM app.entities"))
        ).all()
    return Snapshot(
        facts=[
            FactRow(
                entity=r.entity,
                predicate=r.predicate,
                qualifier=r.qualifier,
                kind=r.kind,
                assertion=r.assertion,
                status=r.status,
                statement=r.statement,
                value_json=r.value_json,
                chained=r.chained,
                pinned=r.pinned,
                domain=r.domain,
            )
            for r in facts
        ],
        reviews=[
            ReviewRow(kind=r.kind, summary=r.summary, status=r.status, domain=r.domain)
            for r in reviews
        ],
        entities=[EntityRow(name=r.name, kind=r.kind, status=r.status) for r in entities],
    )


async def run_scenario(maker: async_sessionmaker, scenario: Scenario) -> Snapshot:
    """Apply every step in order through the real pipeline; return the graph."""
    note_ids: list[str] = []
    domains: list[str] = []
    for step in scenario.steps:
        if step.reanalyze_step is not None:
            # Re-analysis of an earlier step's note: same row, same chunk,
            # same reported_at (and same domain) — only the extraction/intent
            # changes.
            note_id = note_ids[step.reanalyze_step]
            domain = domains[step.reanalyze_step]
        else:
            note_id = await _seed_note(maker, step)
            domain = step.domain
        note_ids.append(note_id)
        domains.append(domain)
        # The intent is compiled against the graph AS IT STANDS now (prior steps
        # committed), so an existing-mode reference resolves to the live entity.
        intent_json = await _compile_intent(maker, step, domain)
        await _integrator(
            maker, json.dumps(step.extraction), intent_json
        ).integrate_note({"note_id": note_id})
    return await _snapshot(maker)


# --- CLI: interactive "be the model" against a standing DB ------------------


def _print_prompt() -> None:
    from jbrain.analysis.prompt import SYSTEM_PROMPT, build_user_prompt

    body = (
        "Saw Dr. Patel today, BP was 128/82. She wants me back in 3 months. "
        "Bumped into Sarah from accounting — she just moved to Denver."
    )
    anchor = datetime.fromisoformat("2026-06-10T17:11:00-06:00")
    print("================ SYSTEM PROMPT ================")
    print(SYSTEM_PROMPT)
    print("\n================ USER PROMPT (anchor as the model sees it) ====")
    print(build_user_prompt([body], anchor=anchor, domain="general"))


async def _cli_run(url: str, path: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        scenario = load_scenario(__import__("pathlib").Path(path))
        snap = await run_scenario(maker, scenario)
        for f in snap.facts:
            print(
                f"  {f.entity}.{f.predicate} [{f.kind}/{f.assertion}/{f.status}]"
                f" {f.statement!r} chained={f.chained} domain={f.domain}"
            )
        for r in snap.reviews:
            print(f"  REVIEW [{r.kind}/{r.status}] {r.summary} (domain={r.domain})")
        failures = check(snap, scenario.expect)
        if failures:
            print("\nFAIL:")
            for msg in failures:
                print(f"  - {msg}")
            return 1
        print("\nPASS")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    import os

    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"
    if mode == "prompt":
        _print_prompt()
        return 0
    if mode == "run":
        url = os.environ["JBRAIN_DATABASE_URL"]
        return asyncio.run(_cli_run(url, sys.argv[2]))
    print(f"unknown mode {mode!r}; use 'prompt' or 'run FILE'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
