"""Run a Scenario against a real Postgres through the genuine analyze_note
pipeline, then snapshot the graph for the checker.

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
import sys
import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
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


def _analyzer(maker: async_sessionmaker, extraction_json: str) -> AnalysisPipeline:
    """A pipeline whose note.extract returns exactly this scenario step's JSON
    (we are the model); routing/tasks mirror the real default."""
    router = LlmRouter(
        {"xai": FakeLlmClient([extraction_json])},
        {"note.extract": ("xai", "grok-4.3")},
    )
    return AnalysisPipeline(maker, router)


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
    import json

    for step in scenario.steps:
        note_id = await _seed_note(maker, step)
        await _analyzer(maker, json.dumps(step.extraction)).analyze_note({"note_id": note_id})
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
