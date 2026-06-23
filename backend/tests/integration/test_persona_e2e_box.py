"""End-to-end calibration against the OWNER'S BOX: feed a year of persona notes
CHRONOLOGICALLY through the REAL pipeline and watch the entity graph build.

This is the stateful half of docs/CALIBRATION_LOOP.md that the per-layer eval
scorers do NOT cover: each note is extracted (box) and integrated (box) and
APPLIED to a real Postgres graph, so note N+1's integrator sees the entities and
facts note N committed. It exercises what integration actually is — resolving a
mention to an EXISTING entity, superseding a prior fact on the SAME node, holding
cross-subject/ambiguous for review — which a static per-case graph_context fixture
cannot.

Owner-run only and SKIPPED by default (CI is box-free): set JBRAIN_BOX_E2E=1 and
JBRAIN_DEBUG_TOKEN=<minted payload>, and point JBRAIN_PERSONA_CORPUS at the corpus
JSON. JBRAIN_BOX_E2E_LIMIT caps the note count for a quick slice. One note at a
time — single GPU.

  cd backend && JBRAIN_BOX_E2E=1 JBRAIN_DEBUG_TOKEN=<payload> \
    JBRAIN_PERSONA_CORPUS=/abs/persona_corpus.json JBRAIN_BOX_E2E_LIMIT=8 \
    uv run pytest tests/integration/test_persona_e2e_box.py -s -q
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from jbrain.analysis.pipeline import AnalysisPipeline
from tests.integration.test_extraction_pg import maker  # noqa: F401  (shared PG fixture)

pytestmark = pytest.mark.skipif(
    not os.environ.get("JBRAIN_BOX_E2E"),
    reason="owner-run box E2E (set JBRAIN_BOX_E2E=1 + JBRAIN_DEBUG_TOKEN + JBRAIN_PERSONA_CORPUS)",
)


def _canonical_domain(note: dict) -> str:
    """Map a corpus note's freeform domain tags onto the four the pipeline knows."""
    ds = set(note.get("domains", []))
    if ds & {"health", "medical"}:
        return "health"
    if ds & {"finance", "money", "insurance"}:
        return "finance"
    if "location" in ds:
        return "location"
    return "general"


async def _seed_note(maker, note: dict) -> str:  # noqa: F811
    """Insert one note + one chunk (body verbatim) with the note's capture time and
    local offset — mirrors tests.harness.runner._seed_note so the temporal anchor
    and firewall behave exactly as a real capture."""
    note_id = str(uuid.uuid4())
    created = datetime.fromisoformat(note["date"])
    offset = created.utcoffset()
    tz = int(offset.total_seconds() // 60) if offset is not None else None
    domain = _canonical_domain(note)
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at,"
                " tz_offset_minutes) VALUES (:i, :c, :d, :b, :t, :tz)"
            ),
            {
                "i": note_id, "c": note_id[:12], "d": domain,
                "b": note["body"], "t": created, "tz": tz,
            },
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i, :n, :d, 'paragraph', 1, :b)"
            ),
            {"i": str(uuid.uuid4()), "n": note_id, "d": domain, "b": note["body"]},
        )
        await s.commit()
    return note_id


async def _tally(maker) -> tuple[int, int, int]:  # noqa: F811
    """A cheap running count — (entities, active facts, superseded) — printed after
    each note so a long run shows the graph GROWING, not just notes consumed."""
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        ents = (
            await s.execute(text("SELECT count(*) FROM app.entities WHERE status <> 'merged'"))
        ).scalar_one()
        facts = (await s.execute(text("SELECT count(*) FROM app.facts"))).scalar_one()
        sup = (
            await s.execute(text("SELECT count(*) FROM app.facts WHERE superseded_by IS NOT NULL"))
        ).scalar_one()
    return ents, facts, sup


async def _snapshot(maker) -> dict:  # noqa: F811
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        entities = (
            await s.execute(
                text(
                    "SELECT canonical_name, kind, status, domain_code FROM app.entities"
                    " WHERE status <> 'merged' ORDER BY canonical_name"
                )
            )
        ).all()
        facts = (
            await s.execute(
                text(
                    "SELECT e.canonical_name AS entity, f.predicate, f.status,"
                    " f.superseded_by IS NOT NULL AS superseded, f.domain_code"
                    " FROM app.facts f JOIN app.entities e ON e.id = f.entity_id"
                )
            )
        ).all()
        reviews = (await s.execute(text("SELECT kind, status FROM app.review_items"))).all()
    return {"entities": entities, "facts": facts, "reviews": reviews}


async def test_persona_year_builds_the_entity_graph(maker) -> None:  # noqa: F811
    corpus_path = os.environ.get("JBRAIN_PERSONA_CORPUS")
    assert corpus_path, "set JBRAIN_PERSONA_CORPUS to the corpus JSON"
    notes = json.loads(Path(corpus_path).read_text())  # noqa: ASYNC240  (one-time fixture read)
    limit = os.environ.get("JBRAIN_BOX_E2E_LIMIT")
    if limit:
        notes = notes[: int(limit)]

    from evals.box.client import DebugRouter

    router = DebugRouter()
    # DebugRouter is a duck-typed router shim (complete/spec/effective_spec over the
    # box); the pipeline only uses those three, so passing it is sound for the test.
    pipeline = AnalysisPipeline(maker, router)  # type: ignore[arg-type]
    fed = 0
    try:
        for note in notes:
            note_id = await _seed_note(maker, note)
            # integrate_note runs the shared extract front-half (box) then the
            # integrator (box) and applies the intent — committing to the graph.
            await pipeline.integrate_note({"note_id": note_id})
            fed += 1
            e, f, sup = await _tally(maker)
            print(
                f"  fed [{fed}/{len(notes)}] {note['id']} ({_canonical_domain(note)}) "
                f"{note['title']}  → graph: {e} entities, {f} facts, {sup} superseded",
                flush=True,
            )
    finally:
        await router.aclose()

    snap = await _snapshot(maker)
    ents, facts, reviews = snap["entities"], snap["facts"], snap["reviews"]
    superseded = [f for f in facts if f.superseded]
    health_facts = [f for f in facts if f.domain_code == "health"]

    print(f"\n=== GRAPH after {fed} notes ===")
    print(f"entities: {len(ents)} | facts: {len(facts)} | superseded: {len(superseded)} |"
          f" review items: {len(reviews)} | health-domain facts: {len(health_facts)}")
    print("entities:", ", ".join(f"{e.canonical_name}({e.kind})" for e in ents))

    # Robust accumulation invariants (a non-deterministic model run, so assert
    # structure, not exact counts): the graph is non-empty, the owner is a single
    # deduplicated node, and the firewall floored health facts to the health domain.
    assert ents and facts, "the pipeline committed no graph"
    assert sum(e.canonical_name == "Me" for e in ents) <= 1, "owner entity duplicated"
    if any(_canonical_domain(n) == "health" for n in notes):
        assert health_facts, "a health note committed no health-domain fact (firewall)"
