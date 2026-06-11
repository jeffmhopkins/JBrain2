"""Worker loop behavior with the queue faked out: claim/complete/fail wiring,
startup backfill, and engine cleanup. Real SQL behavior is integration-tested."""

import asyncio
from typing import Any

import pytest

from jbrain import queue, worker
from jbrain.queue import Job


class FakeQueue:
    def __init__(self, jobs: list[Job] | None = None):
        self.jobs = list(jobs or [])
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.permanent: list[str] = []
        self.backfills = 0
        self.embed_backfills = 0
        self.purge_backfills = 0
        self.backfill_error: Exception | None = None

    async def claim(self, maker: Any, ctx: Any) -> Job | None:
        assert ctx is queue.SYSTEM_CTX
        return self.jobs.pop(0) if self.jobs else None

    async def complete(self, maker: Any, ctx: Any, job_id: str) -> None:
        self.completed.append(job_id)

    async def fail(
        self, maker: Any, ctx: Any, job_id: str, error: str, *, permanent: bool = False
    ) -> None:
        self.failed.append((job_id, error))
        if permanent:
            self.permanent.append(job_id)

    async def backfill_pending_notes(self, maker: Any, ctx: Any) -> int:
        self.backfills += 1
        if self.backfill_error is not None:
            raise self.backfill_error
        return 0

    async def backfill_unembedded_notes(self, maker: Any, ctx: Any) -> int:
        self.embed_backfills += 1
        return 0


def install(monkeypatch: pytest.MonkeyPatch, fake: FakeQueue) -> None:
    for name in (
        "claim",
        "complete",
        "fail",
        "backfill_pending_notes",
        "backfill_unembedded_notes",
    ):
        monkeypatch.setattr(worker.queue, name, getattr(fake, name))

    # The orphan-purge sweep rides the same startup pass; SQL behavior is
    # integration-tested (test_note_purge_pg), so stub it here like the rest.
    async def fake_purge_backfill(maker):  # noqa: ANN001, ANN202
        fake.purge_backfills += 1
        return 0

    monkeypatch.setattr(worker.purge, "backfill_deleted_note_artifacts", fake_purge_backfill)


def job(kind: str = "ingest_note", payload: dict[str, Any] | None = None) -> Job:
    return Job(
        id="job-1", kind=kind, payload=payload or {"note_id": "n1"}, attempts=0, max_attempts=5
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
    fake = FakeQueue([job(kind="analyze_note")])
    install(monkeypatch, fake)

    async def handler(payload: dict[str, Any]) -> None:
        raise queue.PermanentJobError("malformed extraction after re-ask")

    assert await worker.process_one(None, {"analyze_note": handler}) is True  # type: ignore[arg-type]
    assert fake.completed == []
    assert fake.permanent == ["job-1"]


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
    assert fake.embed_backfills == 1  # embed backfill rides the same startup pass


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

    async def capture(maker: Any, handlers: Any) -> None:
        captured.update(handlers)
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "create_async_engine", lambda url: FakeEngine())
    monkeypatch.setattr(worker, "run_loop", capture)
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert set(captured) == {"ingest_note", "embed_note", "analyze_note", "ocr_attachment"}


async def test_run_disposes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(worker, "create_async_engine", lambda url: engine)
    monkeypatch.setattr(worker, "async_sessionmaker", lambda eng, **kw: object())

    async def boom(maker: Any, handlers: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_loop", boom)
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
    assert engine.disposed
