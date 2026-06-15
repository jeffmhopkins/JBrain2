"""The engine pipeline run-log writer composes a kind='pipeline' Run + a RunStep
per enqueued job (no DB — the ORM objects + the scoped session are faked)."""

import uuid
from contextlib import asynccontextmanager
from typing import Any

from jbrain.db.session import SessionContext
from jbrain.models.agent import Run, RunStep
from jbrain.workflow import runlog
from jbrain.workflow.runlog import EnqueuedStep, PipelineRunLog

PID = "11111111-1111-1111-1111-111111111111"
TRIG = "22222222-2222-2222-2222-222222222222"
JOB1 = "33333333-3333-3333-3333-333333333333"
JOB2 = "44444444-4444-4444-4444-444444444444"


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def _patch_scoped(monkeypatch: Any, session: FakeSession) -> None:
    @asynccontextmanager
    async def scoped(maker: Any, ctx: Any):  # noqa: ANN202
        yield session

    monkeypatch.setattr(runlog, "scoped_session", scoped)


async def test_record_writes_a_pipeline_run_and_a_step_per_job(monkeypatch: Any) -> None:
    session = FakeSession()
    _patch_scoped(monkeypatch, session)

    run_id = await PipelineRunLog(None).record(  # type: ignore[arg-type]
        SessionContext(principal_kind="owner"),
        pipeline="event_integrate_note",
        trigger_id=TRIG,
        ran_as="scoped",
        domain_code="general",
        principal_id=PID,
        steps=[EnqueuedStep(kind="integrate_note", job_id=JOB1)],
    )

    runs = [o for o in session.added if isinstance(o, Run)]
    steps = [o for o in session.added if isinstance(o, RunStep)]
    assert len(runs) == 1 and len(steps) == 1
    run = runs[0]
    assert run.kind == "pipeline"
    assert run.pipeline == "event_integrate_note"
    assert run.trigger_id == uuid.UUID(TRIG)
    assert run.ran_as == "scoped"
    assert run.domain_code == "general"
    assert run.principal_id == uuid.UUID(PID)
    assert run.status == "done"
    assert run.step_count == 1
    # The step references the enqueued job and is stamped kind='action'.
    assert steps[0].run_id == uuid.UUID(run_id)
    assert steps[0].idx == 0
    assert steps[0].kind == "action"
    assert steps[0].name == "integrate_note"
    assert steps[0].job_id == uuid.UUID(JOB1)
    assert steps[0].ok is True


async def test_record_system_run_omits_scope_and_indexes_multiple_steps(monkeypatch: Any) -> None:
    session = FakeSession()
    _patch_scoped(monkeypatch, session)

    await PipelineRunLog(None).record(  # type: ignore[arg-type]
        SessionContext(principal_kind="owner"),
        pipeline="p",
        trigger_id=None,
        ran_as="system",
        domain_code=None,
        principal_id=None,
        steps=[
            EnqueuedStep(kind="a", job_id=JOB1),
            EnqueuedStep(kind="b", job_id=JOB2),
        ],
    )

    run = next(o for o in session.added if isinstance(o, Run))
    steps = [o for o in session.added if isinstance(o, RunStep)]
    assert run.ran_as == "system"
    assert run.trigger_id is None
    assert run.domain_code is None
    assert run.principal_id is None
    assert run.step_count == 2
    # Steps are indexed in order and carry their own job ids.
    assert [(s.idx, s.name, s.job_id) for s in steps] == [
        (0, "a", uuid.UUID(JOB1)),
        (1, "b", uuid.UUID(JOB2)),
    ]
