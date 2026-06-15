"""integrate_note's net-new run + resolution-pin persistence against real Postgres
(docs/WORKFLOW_ENGINE_PLAN.md §E7b, Wave 1 Track A).

Net-new has no shadow baseline (the loop logged to structlog only), so it is
validated by CONVERGENCE: integrating the same note twice yields IDENTICAL
resolution_pin rows (idempotent upsert, never duplicates) and exactly one
integration run per call. Plus: the run row carries kind='integration',
ran_as='system', and the note's domain; and the pins stay domain-firewalled (a
cross-domain session can't read another domain's pins). Both model calls are
faked. The gate (integration_persist, default ON) is exercised by also asserting
that flipping it OFF writes nothing.
"""

import json
import uuid

import pytest
from sqlalchemy import text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.queue import SYSTEM_CTX
from jbrain.settings_store import INTEGRATION_PERSIST_KEY, SqlSettingsStore
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _extract(name: str) -> str:
    return json.dumps(
        {
            "title": "Work",
            "tags": ["work"],
            "mentions": [{"name": name, "kind": "Organization", "surface_text": name}],
            "facts": [
                {
                    "entity_ref": name,
                    "predicate": "industry",
                    "qualifier": "",
                    "kind": "attribute",
                    "statement": f"{name} is in tech",
                    "value_json": None,
                    "assertion": "asserted",
                    "object_entity_ref": None,
                    "domain": "general",
                    "temporal": None,
                }
            ],
            "temporal_tokens": [],
        }
    )


def _intent(name: str, *, existing_id: str | None = None) -> str:
    # A re-run's agent resolves the SAME mention to the SAME existing entity (the
    # entity it minted last time now reaches it via graph context). Modelling that
    # — mode='existing' with the first run's id — is what makes the IDENTITY pin
    # convergent; a static mode='new' fixture would mint a fresh entity each run and
    # the pin's entity_id would (correctly) churn, because the pin records the
    # committed decision and the decision itself changed.
    resolution: dict = {"mention_ref": name, "surface": name}
    if existing_id is None:
        resolution.update({"mode": "new", "new_kind": "Organization", "new_name": name})
    else:
        resolution.update({"mode": "existing", "entity_id": existing_id})
    return json.dumps(
        {
            "resolutions": [resolution],
            "facts": [
                {
                    "entity_ref": name,
                    "predicate": "industry",
                    "kind": "attribute",
                    "assertion": "asserted",
                    "statement": f"{name} is in tech",
                    "self_confidence": 0.95,
                    "surface": name,  # present in the body -> committed -> predicate_key pin
                }
            ],
        }
    )


def _pipeline(maker, name: str, *, existing_id: str | None = None) -> AnalysisPipeline:  # noqa: F811
    # A fresh fake per integrate call: the two responses (extract, integrate) are
    # consumed in order, so a second integrate needs its own scripted pair.
    fake = FakeLlmClient(responses=[_extract(name), _intent(name, existing_id=existing_id)])
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )
    return AnalysisPipeline(maker, router, settings=SqlSettingsStore(maker))


async def _entity_id(maker, name: str) -> str:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(
                text("SELECT id::text FROM app.entities WHERE canonical_name = :n"),
                {"n": name},
            )
        ).scalar_one()


async def _owner(maker) -> None:  # noqa: F811
    # The integration run is stamped to the owner principal; seed one.
    await service.rotate_owner_key(SqlAuthRepo(maker))


async def _pins(maker, note_id: str) -> list[tuple]:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT chunk_id::text, occurrence_index, decision_kind, surface,"
                    " span_text_hash, entity_id::text, normalized_predicate, domain_code"
                    " FROM app.resolution_pin WHERE note_id = :nid"
                    " ORDER BY decision_kind, occurrence_index"
                ),
                {"nid": note_id},
            )
        ).all()
    return [tuple(r) for r in rows]


async def _integration_runs(maker) -> list[tuple]:  # noqa: F811
    """Every integration run + its step count. The shared module DB accumulates
    runs across tests, so callers compare counts as a DELTA, never absolute."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT r.id::text, r.kind, r.ran_as, r.domain_code, r.status,"
                    " r.stop_reason, r.step_count,"
                    " (SELECT count(*) FROM app.run_steps st WHERE st.run_id = r.id)"
                    " FROM app.runs r WHERE r.kind = 'integration'"
                )
            )
        ).all()
    return [tuple(r) for r in rows]


async def test_reintegration_converges_to_identical_pins_and_one_run_per_call(maker, tmp_path):  # noqa: F811
    await _owner(maker)
    name = f"Globex {uuid.uuid4().hex[:8]}"  # isolate this note's rows in the shared DB
    note_id = await make_note(maker, domain="general", body=f"{name} is in tech.")
    await ingest(maker, note_id, tmp_path)

    before = {r[0] for r in await _integration_runs(maker)}
    await _pipeline(maker, name).integrate_note({"note_id": note_id})
    first_pins = await _pins(maker, note_id)

    # A predicate_key pin (the committed fact) and an identity pin (the attested,
    # committed resolution) both land, keyed on the same surface occurrence.
    kinds = sorted(p[2] for p in first_pins)
    assert kinds == ["identity", "predicate_key"]

    # The re-run resolves the mention to the entity the first run minted (mode
    # existing) — exactly how production graph context steers a re-integration.
    existing_id = await _entity_id(maker, name)
    await _pipeline(maker, name, existing_id=existing_id).integrate_note({"note_id": note_id})
    second_pins = await _pins(maker, note_id)

    # Convergence: the SAME pins, not duplicated (idempotent upsert).
    assert second_pins == first_pins
    assert len(second_pins) == 2

    # One integration run per call: two calls -> exactly two NEW runs (delta against
    # the shared DB), each stamped correctly with three steps persisted.
    new_runs = [r for r in await _integration_runs(maker) if r[0] not in before]
    assert len(new_runs) == 2
    for _id, kind, ran_as, domain_code, status, stop_reason, step_count, n_steps in new_runs:
        assert kind == "integration"
        assert ran_as == "system"  # owner-system, recorded (E1), not a smuggled escalation
        assert domain_code == "general"  # the note's domain
        assert status == "done"
        assert stop_reason == "committed"
        assert step_count == 3 == n_steps  # extraction -> integration -> arbiter, all persisted


async def test_pins_are_domain_firewalled_to_their_note(maker, tmp_path):  # noqa: F811
    await _owner(maker)
    name = f"HealthCo {uuid.uuid4().hex[:8]}"
    note_id = await make_note(maker, domain="health", body=f"{name} is in tech.")
    await ingest(maker, note_id, tmp_path)
    await _pipeline(maker, name).integrate_note({"note_id": note_id})

    # The note is health-domain, so its pins carry domain_code='health'.
    health_pins = await _pins(maker, note_id)
    assert health_pins  # the run did persist some
    assert all(p[7] == "health" for p in health_pins)

    # A general-only (cross-domain, non-owner) session sees none of them.
    general_only = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, general_only) as s:
        visible = (
            await s.execute(
                text("SELECT count(*) FROM app.resolution_pin WHERE note_id = :nid"),
                {"nid": note_id},
            )
        ).scalar_one()
    assert visible == 0


async def test_gate_off_persists_nothing(maker, tmp_path):  # noqa: F811
    await _owner(maker)
    settings = SqlSettingsStore(maker)
    await settings.upsert(SYSTEM_CTX, INTEGRATION_PERSIST_KEY, False)
    try:
        name = f"OffCo {uuid.uuid4().hex[:8]}"
        note_id = await make_note(maker, domain="general", body=f"{name} is in tech.")
        await ingest(maker, note_id, tmp_path)
        await _pipeline(maker, name).integrate_note({"note_id": note_id})
        assert await _pins(maker, note_id) == []  # gated off -> no pins
    finally:
        # Restore the ON default so other tests in the shared DB are unaffected.
        await settings.upsert(SYSTEM_CTX, INTEGRATION_PERSIST_KEY, True)


def _rejecting_intent(name: str) -> str:
    # A fact whose entity_ref names no resolution -> fatal unknown_entity_ref ->
    # plan.rejected. The arbiter commits nothing on a rejected plan.
    return json.dumps(
        {
            "resolutions": [],
            "facts": [
                {
                    "entity_ref": "Ghost",  # not in resolutions -> fatal violation
                    "predicate": "industry",
                    "kind": "attribute",
                    "assertion": "asserted",
                    "statement": f"{name} is in tech",
                    "self_confidence": 0.95,
                    "surface": name,
                }
            ],
        }
    )


async def test_rejected_reintegration_does_not_wipe_prior_pins(maker, tmp_path):  # noqa: F811
    await _owner(maker)
    name = f"KeepCo {uuid.uuid4().hex[:8]}"
    note_id = await make_note(maker, domain="general", body=f"{name} is in tech.")
    await ingest(maker, note_id, tmp_path)

    # First, a clean integration persists this note's pins.
    await _pipeline(maker, name).integrate_note({"note_id": note_id})
    before = await _pins(maker, note_id)
    assert before  # the good run did persist pins

    # A later re-integration is REJECTED (a transient structural fault). It must NOT
    # touch the pin table: a rejected plan commits nothing, so wiping the prior
    # converged pins would be a silent flip (N10). The persist is skipped entirely.
    runs_before = {r[0] for r in await _integration_runs(maker)}
    fake = FakeLlmClient(responses=[_extract(name), _rejecting_intent(name)])
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )
    await AnalysisPipeline(maker, router, settings=SqlSettingsStore(maker)).integrate_note(
        {"note_id": note_id}
    )

    assert await _pins(maker, note_id) == before  # prior pins survive untouched
    # No run row is written for a rejected integration (it committed nothing).
    runs_after = {r[0] for r in await _integration_runs(maker)}
    assert runs_after == runs_before
