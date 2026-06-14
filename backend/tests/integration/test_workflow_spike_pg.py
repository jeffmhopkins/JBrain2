"""The DBOS spike against a real Postgres: it proves the engine's claims on a live
runtime and enforces adoption condition #1 (nothing firewalled in the `dbos`
schema). Skipped where Docker is absent — DBOS needs a Postgres system database, so
this is the half of the spike that runs in CI, not in a bare sandbox.

It does not touch any application table or the Alembic chain: DBOS bootstraps its
own `dbos` schema, so the spike stays collision-free with the concurrent
ingestion/entity-graph work."""

from collections.abc import Iterator

import pytest

from tests.conftest import docker_available, pgvector_container

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# DBOS is not yet a committed dependency (deferred to avoid uv.lock churn against
# the concurrent session); skip cleanly if it is not installed.
dbos = pytest.importorskip("dbos")


@pytest.fixture()
def launched_dbos() -> Iterator[object]:
    """Launch DBOS against a fresh test Postgres in its own `dbos` schema, torn
    down fully so each test starts from a clean system database. `pgvector_container`
    is conftest's context-manager helper (not a fixture), mirroring `test_rls`."""
    from dbos import DBOS

    from jbrain.workflow import spike

    with pgvector_container() as pg:
        url = pg.get_connection_url(driver="psycopg")
        DBOS.destroy()
        DBOS(config=spike.build_config(url))
        DBOS.launch()
        try:
            yield spike
        finally:
            DBOS.destroy()


def test_digest_runs_and_fans_out(launched_dbos) -> None:
    """Happy path with no approval needed (1-day window → < 5 entities → gate stays
    closed): gather → fan-out → finalize, returning reference-shaped results."""
    from jbrain.workflow.safety import assert_reference_shaped

    result = launched_dbos.weekly_entity_digest(1)

    assert result["entities"] == 1
    assert result["approved"] is True
    assert all(s.startswith("summary-ref:") for s in result["summaries"])
    assert_reference_shaped(result["summaries"], where="digest summaries")


def test_durable_approval_pause_and_resume(launched_dbos) -> None:
    """The make-or-break primitive: start a run that gates on approval, confirm it
    parks waiting, then wake it by workflow ID and confirm it resumes approved."""
    from dbos import DBOS

    handle = DBOS.start_workflow(launched_dbos.weekly_entity_digest, 7)

    # It should be pending review, not finished, until the owner decides.
    pending = DBOS.get_event(handle.workflow_id, launched_dbos.PENDING_EVENT, timeout_seconds=10)
    assert pending == {"count": 7}

    launched_dbos.approve(handle.workflow_id, "approved")
    result = handle.get_result()
    assert result["approved"] is True


def test_no_firewalled_payload_in_system_schema(launched_dbos) -> None:
    """Adoption condition #1, enforced on the real store: after a run, scan the
    serialized inputs/outputs DBOS persisted and assert every one is
    reference-shaped — i.e. no note/LLM content leaked into the `dbos` schema."""
    from dbos import DBOS

    from jbrain.workflow.safety import is_reference_shaped

    handle = DBOS.start_workflow(launched_dbos.weekly_entity_digest, 1)
    handle.get_result()

    leaked: list[str] = []
    for wf in DBOS.list_workflows(load_input=True, load_output=True):
        for label, payload in (("input", wf.input), ("output", wf.output)):
            if payload is not None and not is_reference_shaped(payload):
                leaked.append(f"{wf.name}.{label}")
    assert not leaked, f"content-shaped payloads reached the dbos schema: {leaked}"
