"""A self-contained DBOS spike — proves the engine's load-bearing primitives.

This is a *spike*, not the production engine: the steps operate on synthetic IDs,
not real entities, precisely so it touches none of the concurrent
ingestion/entity-graph work. Its job is to validate, on a real DBOS runtime, the
four things the research said make or break the DBOS bet, plus the two adoption
caveats:

1. **Durable multi-day human approval** — `set_event` + `recv` (the wiki
   split/merge gate). A paused workflow survives process restarts.
2. **Conditional gating** — branch on a step result (the ingest OCR gate shape).
3. **Fan-out with bounded concurrency** — a `Queue` per item (chunk→embed shape).
4. **Scheduling** — `@DBOS.scheduled` (the nightly build).
5. **Determinism discipline (caveat #2)** — every nondeterministic effect is a
   `@DBOS.step`; the workflow body only sequences and branches on step outputs.
6. **IDs-not-payloads (caveat #1)** — steps take and return references only; the
   integration test asserts nothing content-shaped reaches the `dbos` schema.

DBOS requires a Postgres system database, so nothing here runs at import time. The
testcontainers integration test calls `launch()` and drives these workflows.
"""

from __future__ import annotations

from dbos import DBOS, DBOSConfig, Queue
from pydantic import BaseModel

from jbrain.workflow.registry import BlockRegistry, block
from jbrain.workflow.safety import assert_reference_shaped

# The spike's slice of the shared block library. Real blocks register the same way.
BLOCKS = BlockRegistry()

# Bounded fan-out: cap concurrent "summaries" the way real embedding would cap
# GPU/API pressure, instead of spawning one unbounded task per item.
DIGEST_QUEUE = Queue("spike_digest", concurrency=4)

# Topic + event key the approval gate rendezvous on. The owner-facing endpoint
# correlates its decision back to a paused run by workflow ID (see `approve`).
APPROVAL_TOPIC = "spike_approval"
PENDING_EVENT = "pending_review"


class _SinceParams(BaseModel):
    since_days: int = 7


class _EntityParams(BaseModel):
    entity_id: str


@block(BLOCKS, name="recent_entity_ids", version=1, params=_SinceParams,
       domains=("general",), description="IDs of entities touched in the window.")
@DBOS.step()
def recent_entity_ids(since_days: int) -> list[str]:
    """Stand-in for an RLS-scoped query. Returns IDs only — never entity rows. A
    wider window yields more entities, so the review gate below depends on input."""
    return [f"entity-{since_days:02d}-{i}" for i in range(min(since_days, 8))]


@block(BLOCKS, name="needs_review", version=1, params=_SinceParams,
       domains=("general",), description="Whether this digest needs owner sign-off.")
@DBOS.step()
def needs_review(entity_ids: list[str]) -> bool:
    """The conditional gate. A pure decision over a reference list."""
    return len(entity_ids) >= 5


@block(BLOCKS, name="summarize_entity", version=1, params=_EntityParams, kind="llm",
       domains=("general",), description="Summarize one entity; returns a ref.")
@DBOS.step(retries_allowed=True, max_attempts=3)
def summarize_entity(entity_id: str) -> str:
    """Stand-in for an LLM-adapter call. Idempotent and reference-shaped: it returns
    the ID of where a summary would be written, not the prose."""
    return f"summary-ref:{entity_id}"


@DBOS.workflow()
def weekly_entity_digest(since_days: int = 7) -> dict[str, object]:
    """Compose the blocks into one durable run: gather → gate → fan-out → (maybe)
    pause for approval → finalize. The body is deterministic; all side effects are
    steps, so a crash anywhere resumes from the last checkpoint."""
    entity_ids = recent_entity_ids(since_days)
    assert_reference_shaped(entity_ids, where="recent_entity_ids output")

    handles = [DIGEST_QUEUE.enqueue(summarize_entity, eid) for eid in entity_ids]
    summaries = [h.get_result() for h in handles]

    approved = True
    if needs_review(entity_ids):
        # Surface to the review inbox, then block durably (hours→days) until the
        # owner decides. Survives restarts: recovery replays to this same recv.
        DBOS.set_event(PENDING_EVENT, {"count": len(entity_ids)})
        decision = DBOS.recv(APPROVAL_TOPIC, timeout_seconds=7 * 24 * 3600)
        approved = decision == "approved"

    return {"entities": len(entity_ids), "summaries": summaries, "approved": approved}


@DBOS.scheduled("0 2 * * 1")  # 02:00 every Monday
@DBOS.workflow()
def scheduled_weekly_digest(scheduled_at: object, actual_at: object) -> None:
    """The scheduled trigger. Exactly-once-per-interval recovery is DBOS's job; the
    same workflow is also directly callable for an emergency/manual run."""
    weekly_entity_digest(7)


def approve(workflow_id: str, decision: str) -> None:
    """What the owner-facing approval endpoint calls: wake the paused run by ID.
    Decoupling the decision from the workflow is the whole point of `recv`."""
    DBOS.send(workflow_id, decision, topic=APPROVAL_TOPIC)


def build_config(database_url: str) -> DBOSConfig:
    """Pin DBOS into its own `dbos` schema on the *same* Postgres — one database,
    quarantined namespace (adoption condition #3: Alembic owns `public`, `dbos
    migrate` owns `dbos`)."""
    return DBOSConfig(
        name="jbrain-workflow-spike",
        database_url=database_url,
        dbos_system_schema="dbos",
        run_admin_server=False,
    )
