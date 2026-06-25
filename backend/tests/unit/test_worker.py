"""Worker loop behavior with the queue faked out: claim/complete/fail wiring,
startup backfill, and engine cleanup. Real SQL behavior is integration-tested."""

import asyncio
from typing import Any

import pytest

from jbrain import queue, worker
from jbrain.queue import Job


async def _noop_progress(_note: str) -> None:
    return None


async def test_invoke_passes_only_the_extras_a_handler_declares() -> None:
    seen: dict[str, Any] = {}

    async def payload_only(payload: dict[str, Any]) -> None:
        seen["payload_only"] = payload

    async def with_ctx(payload: dict[str, Any], ctx: Any) -> None:
        seen["ctx"] = ctx

    async def with_progress(payload: dict[str, Any], *, progress: Any) -> None:
        await progress("processed 1 of 1 emails")
        seen["progress"] = progress

    notes: list[str] = []

    async def reporter(note: str) -> None:
        notes.append(note)

    await worker._invoke(payload_only, {"a": 1}, queue.SYSTEM_CTX, _noop_progress)
    assert seen["payload_only"] == {"a": 1}

    sentinel = object()
    await worker._invoke(with_ctx, {}, sentinel, _noop_progress)  # type: ignore[arg-type]
    assert seen["ctx"] is sentinel

    # A `progress`-declaring handler gets the reporter by keyword (and no ctx, since it
    # has only one positional parameter) and can drive it.
    await worker._invoke(with_progress, {}, queue.SYSTEM_CTX, reporter)
    assert notes == ["processed 1 of 1 emails"]


class FakeQueue:
    def __init__(self, jobs: list[Job] | None = None):
        self.jobs = list(jobs or [])
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.permanent: list[str] = []
        self.backfills = 0
        self.embed_backfills = 0
        self.integration_backfills = 0
        self.consolidate_backfills = 0
        self.predicate_sync_backfills = 0
        self.purge_backfills = 0
        self.backfill_error: Exception | None = None
        # Whether a non-permanent fail() burned the last attempt.
        self.fail_exhausts = False

    async def claim(self, maker: Any, ctx: Any) -> Job | None:
        assert ctx is queue.SYSTEM_CTX
        return self.jobs.pop(0) if self.jobs else None

    async def complete(self, maker: Any, ctx: Any, job_id: str) -> None:
        self.completed.append(job_id)

    async def fail(
        self, maker: Any, ctx: Any, job_id: str, error: str, *, permanent: bool = False
    ) -> bool:
        self.failed.append((job_id, error))
        if permanent:
            self.permanent.append(job_id)
        return permanent or self.fail_exhausts

    async def backfill_pending_notes(self, maker: Any, ctx: Any) -> int:
        self.backfills += 1
        if self.backfill_error is not None:
            raise self.backfill_error
        return 0

    async def backfill_unembedded_notes(self, maker: Any, ctx: Any) -> int:
        self.embed_backfills += 1
        return 0

    async def backfill_pending_integration(self, maker: Any, ctx: Any) -> int:
        self.integration_backfills += 1
        return 0

    async def backfill_consolidate(self, maker: Any, ctx: Any) -> int:
        self.consolidate_backfills += 1
        return 0

    async def backfill_sync_predicates(self, maker: Any, ctx: Any) -> int:
        self.predicate_sync_backfills += 1
        return 0


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeQueue) -> None:
    for name in (
        "claim",
        "complete",
        "fail",
        "backfill_pending_notes",
        "backfill_unembedded_notes",
        "backfill_pending_integration",
        "backfill_consolidate",
        "backfill_sync_predicates",
    ):
        monkeypatch.setattr(worker.queue, name, getattr(fake, name))

    # The orphan-purge sweep rides the same startup pass; SQL behavior is
    # integration-tested (test_note_purge_pg), so stub it here like the rest.
    async def fake_purge_backfill(maker):  # noqa: ANN001, ANN202
        fake.purge_backfills += 1
        return 0

    monkeypatch.setattr(worker.purge, "backfill_deleted_note_artifacts", fake_purge_backfill)


def job(
    kind: str = "ingest_note",
    payload: dict[str, Any] | None = None,
    *,
    principal_id: str | None = None,
    domain_code: str | None = None,
) -> Job:
    return Job(
        id="job-1",
        kind=kind,
        payload=payload or {"note_id": "n1"},
        attempts=0,
        max_attempts=5,
        principal_id=principal_id,
        domain_code=domain_code,
    )


async def test_process_one_runs_handler_and_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueue([job()])
    install(monkeypatch, fake)
    seen: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> None:
        seen.append(payload)

    assert await worker.process_one(None, {"ingest_note": handler}) is True  # type: ignore[arg-type]
    assert seen == [{"note_id": "n1"}]
    assert fake.completed == ["job-1"]
    assert fake.failed == []


async def test_unstamped_job_runs_under_system_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    """The six shipped kinds carry no stamp: the worker runs them under SYSTEM_CTX
    exactly as before — the regression guard for E1 not touching system jobs."""
    fake = FakeQueue([job()])
    install(monkeypatch, fake)
    seen_ctx: list[Any] = []

    async def handler(payload: dict[str, Any], ctx: Any) -> None:  # opts into the ctx
        seen_ctx.append(ctx)

    assert await worker.process_one(None, {"ingest_note": handler}) is True  # type: ignore[arg-type]
    assert seen_ctx == [queue.SYSTEM_CTX]
    assert fake.completed == ["job-1"]


async def test_stamped_job_runs_under_narrowed_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stamped job's handler runs under the narrowed (owner_scoped + single-domain)
    context built from the stamp, not the all-domains SYSTEM_CTX (E1)."""
    fake = FakeQueue([job(principal_id="prince-1", domain_code="health")])
    install(monkeypatch, fake)
    seen_ctx: list[Any] = []

    async def handler(payload: dict[str, Any], ctx: Any) -> None:
        seen_ctx.append(ctx)

    assert await worker.process_one(None, {"ingest_note": handler}) is True  # type: ignore[arg-type]
    assert len(seen_ctx) == 1
    ctx = seen_ctx[0]
    assert ctx is not queue.SYSTEM_CTX
    assert ctx.owner_scoped is True
    assert ctx.principal_id == "prince-1"
    assert tuple(ctx.domain_scopes) == ("health",)
    assert fake.completed == ["job-1"]


async def test_partial_stamp_fails_closed_without_running_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-stamped job (principal set, domain dropped) is failed PERMANENTLY and
    the handler never runs — no silent widening to SYSTEM_CTX (fail-closed E1)."""
    fake = FakeQueue([job(principal_id="prince-1", domain_code=None)])
    install(monkeypatch, fake)
    ran = False

    async def handler(payload: dict[str, Any]) -> None:
        nonlocal ran
        ran = True

    assert await worker.process_one(None, {"ingest_note": handler}) is True  # type: ignore[arg-type]
    assert ran is False  # the handler was never invoked
    assert fake.completed == []
    assert fake.permanent == ["job-1"]  # failed permanently, not retried


async def test_resolve_exec_context_maps_stamp_to_scope() -> None:
    """The pure mapping the worker uses: unstamped → SYSTEM_CTX, stamped → narrowed."""
    assert worker.resolve_exec_context(job()) is queue.SYSTEM_CTX
    scoped = worker.resolve_exec_context(job(principal_id="p", domain_code="finance"))
    assert scoped is not queue.SYSTEM_CTX
    assert scoped.owner_scoped is True and tuple(scoped.domain_scopes) == ("finance",)


async def test_process_one_fails_job_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQueue([job()])
    install(monkeypatch, fake)

    async def handler(payload: dict[str, Any]) -> None:
        raise RuntimeError("pdf exploded")

    assert await worker.process_one(None, {"ingest_note": handler}) is True  # type: ignore[arg-type]
    assert fake.completed == []
    assert fake.failed == [("job-1", "RuntimeError('pdf exploded')")]


async def test_process_one_fails_permanently_on_permanent_job_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQueue([job(kind="integrate_note")])
    install(monkeypatch, fake)

    async def handler(payload: dict[str, Any]) -> None:
        raise queue.PermanentJobError("malformed extraction after re-ask")

    assert await worker.process_one(None, {"integrate_note": handler}) is True  # type: ignore[arg-type]
    assert fake.completed == []
    assert fake.permanent == ["job-1"]


def install_fallback_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def spy(maker: Any, attachment_id: str) -> str | None:
        calls.append(attachment_id)
        return "fallback-job"

    monkeypatch.setattr(worker.ocr, "enqueue_analysis_fallback", spy)
    return calls


async def boom_handler(payload: dict[str, Any]) -> None:
    raise RuntimeError("vision call kept failing")


async def test_exhausted_ocr_job_triggers_analysis_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQueue([job(kind="ocr_attachment", payload={"attachment_id": "att-1"})])
    fake.fail_exhausts = True
    install(monkeypatch, fake)
    calls = install_fallback_spy(monkeypatch)

    assert await worker.process_one(None, {"ocr_attachment": boom_handler}) is True  # type: ignore[arg-type]
    assert calls == ["att-1"]


async def test_permanent_ocr_failure_also_triggers_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQueue([job(kind="ocr_attachment", payload={"attachment_id": "att-2"})])
    install(monkeypatch, fake)
    calls = install_fallback_spy(monkeypatch)

    async def permanent(payload: dict[str, Any]) -> None:
        raise queue.PermanentJobError("oversized after all")

    assert await worker.process_one(None, {"ocr_attachment": permanent}) is True  # type: ignore[arg-type]
    assert fake.permanent == ["job-1"]
    assert calls == ["att-2"]


async def test_exhausted_transcribe_job_triggers_analysis_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The audio twin of the OCR fallback: a permanently-failed transcription must
    # not strand its note unanalyzed (worker._after_exhaustion spans both kinds).
    fake = FakeQueue([job(kind="transcribe_attachment", payload={"attachment_id": "att-a"})])
    fake.fail_exhausts = True
    install(monkeypatch, fake)
    calls = install_fallback_spy(monkeypatch)

    assert await worker.process_one(None, {"transcribe_attachment": boom_handler}) is True  # type: ignore[arg-type]
    assert calls == ["att-a"]


async def test_non_exhausted_ocr_failure_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Retries remain: the job will run again, so analysis must keep waiting.
    fake = FakeQueue([job(kind="ocr_attachment", payload={"attachment_id": "att-3"})])
    install(monkeypatch, fake)
    calls = install_fallback_spy(monkeypatch)

    assert await worker.process_one(None, {"ocr_attachment": boom_handler}) is True  # type: ignore[arg-type]
    assert fake.failed and calls == []


async def test_exhausted_non_ocr_job_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQueue([job(kind="integrate_note")])
    fake.fail_exhausts = True
    install(monkeypatch, fake)
    calls = install_fallback_spy(monkeypatch)

    assert await worker.process_one(None, {"integrate_note": boom_handler}) is True  # type: ignore[arg-type]
    assert calls == []


async def test_process_one_fails_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueue([job(kind="mystery")])
    install(monkeypatch, fake)
    assert await worker.process_one(None, {}) is True  # type: ignore[arg-type]
    assert fake.failed and "mystery" in fake.failed[0][1]


async def test_process_one_reports_idle_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeQueue())
    assert await worker.process_one(None, {}) is False  # type: ignore[arg-type]


async def test_run_loop_backfills_once_then_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueue([job()])
    install(monkeypatch, fake)
    done: list[str] = []

    async def handler(payload: dict[str, Any]) -> None:
        done.append(payload["note_id"])

    sleeps = 0

    async def fake_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:  # let it idle twice to prove backfill doesn't repeat
            raise asyncio.CancelledError

    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_loop(None, {"ingest_note": handler})  # type: ignore[arg-type]
    assert done == ["n1"]
    assert fake.backfills == 1
    # Embed/integration/purge backfills all ride the same once-per-boot pass.
    assert fake.embed_backfills == 1
    assert fake.integration_backfills == 1
    assert fake.purge_backfills == 1
    assert fake.consolidate_backfills == 1
    assert fake.predicate_sync_backfills == 1


async def test_run_loop_samples_and_maintains_metrics_when_supervisor_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQueue()
    install(monkeypatch, fake)
    calls: dict[str, Any] = {"samples": 0, "rollup_windows": [], "prunes": 0}

    async def fake_sample(maker: Any, ctx: Any, client: Any, token: str) -> bool:
        assert ctx is queue.SYSTEM_CTX
        calls["samples"] += 1
        return True

    async def fake_rollup(maker: Any, ctx: Any, *, window: Any) -> int:
        calls["rollup_windows"].append(window)
        return 0

    async def fake_prune(maker: Any, ctx: Any) -> tuple[int, int]:
        calls["prunes"] += 1
        return (0, 0)

    monkeypatch.setattr(worker.ops_metrics, "sample_once", fake_sample)
    monkeypatch.setattr(worker.ops_metrics, "rollup", fake_rollup)
    monkeypatch.setattr(worker.ops_metrics, "prune", fake_prune)
    # Pin the clock past both intervals so the first iteration fires deterministically:
    # last_sample/last_maintenance start at 0, and a freshly-booted runner's real
    # monotonic() can be < the 300s maintenance interval (the CI-vs-local difference).
    monkeypatch.setattr(worker.time, "monotonic", lambda: 1_000_000.0)

    async def fake_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_loop(
            None,  # type: ignore[arg-type]
            {},
            supervisor_client=object(),  # type: ignore[arg-type]
            supervisor_token="t",
        )
    # First pass samples once and runs the boot maintenance (full-window rollup).
    assert calls["samples"] == 1
    assert calls["rollup_windows"] == [worker.ops_metrics.RAW_RETENTION]
    assert calls["prunes"] == 1


async def test_run_loop_skips_metrics_without_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeQueue()
    install(monkeypatch, fake)
    sampled = False

    async def fake_sample(*args: Any, **kwargs: Any) -> bool:
        nonlocal sampled
        sampled = True
        return True

    monkeypatch.setattr(worker.ops_metrics, "sample_once", fake_sample)

    async def fake_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_loop(None, {})  # type: ignore[arg-type]
    assert sampled is False  # no supervisor client -> no sampling


async def test_sample_metrics_safely_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args: Any, **kwargs: Any) -> bool:
        raise ConnectionError("supervisor down")

    monkeypatch.setattr(worker.ops_metrics, "sample_once", boom)
    # Must not raise — a supervisor blip is a missed sample, not a worker crash.
    await worker._sample_metrics_safely(None, object(), "t")  # type: ignore[arg-type]


async def test_run_loop_survives_transient_errors_and_retries_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeQueue()
    fake.backfill_error = ConnectionError("db down")
    install(monkeypatch, fake)

    async def fake_sleep(seconds: float) -> None:
        if fake.backfills >= 2:
            raise asyncio.CancelledError
        if fake.backfills == 1:
            fake.backfill_error = None  # DB came back

    monkeypatch.setattr(worker.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_loop(None, {})  # type: ignore[arg-type]
    # First attempt failed, the loop kept going and the retry succeeded.
    assert fake.backfills == 2


async def test_run_registers_all_job_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        async def dispose(self) -> None:
            pass

    captured: dict[str, Any] = {}

    async def capture(
        maker: Any, handlers: Any, registry: Any = None, settings: Any = None, **_: Any
    ) -> None:
        captured.update(handlers)
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "create_async_engine", lambda url: FakeEngine())
    monkeypatch.setattr(worker, "run_loop", capture)
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert set(captured) == {
        "ingest_note",
        "embed_note",
        "integrate_note",
        "ocr_attachment",
        # The audio sibling of ocr_attachment — in-code only, not in ACTION_SPECS /
        # the app.actions seed (docs/WHISPER_TRANSCRIPTION_PLAN.md).
        "transcribe_attachment",
        # The video sibling — in-code only, not in ACTION_SPECS / the app.actions
        # seed (docs/VIDEO_ANALYSIS_PLAN.md).
        "analyze_video_attachment",
        "consolidate_predicates",
        "sync_predicates",
        # The purge sweep is now a fireable action (Phase-5 Track B).
        "purge_deleted_artifacts",
        # The three boot self-heal backfills are now fireable actions too (Phase-5
        # Wave 2 + Track S — the dropped-event safety net): boot + schedule + on-demand.
        "reconcile_pending_notes",
        "reconcile_pending_integration",
        "reconcile_unembedded_notes",
        # The geofence reconciler backstop (Phase 7 Wave 3c) — in-code only, not in
        # ACTION_SPECS / the app.actions seed; a migration seeds its schedule.
        "geofence_sweep",
        # Phase-6 hygiene sweeps — in-code only (a migration seeds the schedules).
        "entity_hygiene",
        "reembed_stale",
        "tag_consolidate",
        # The wiki builder (Phase-6 Wave C2a) — four in-code actions, likewise not
        # in ACTION_SPECS / the app.actions seed.
        "wiki_refresh",
        "wiki_rebuild",
        "wiki_reindex",
        "wiki_prune",
        # The archivist's inbox-triage sweep — in-code only, not in ACTION_SPECS /
        # the app.actions seed; a migration seeds its schedule (docs/EMAIL_ARCHIVIST_PLAN.md).
        "triage_inbox",
    }


async def test_run_disposes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(worker, "create_async_engine", lambda url: engine)
    monkeypatch.setattr(worker, "async_sessionmaker", lambda eng, **kw: object())

    async def boom(
        maker: Any, handlers: Any, registry: Any = None, settings: Any = None, **_: Any
    ) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_loop", boom)
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert engine.disposed
