import asyncio
from typing import Any

import pytest

from jbrain import worker


class FakeConn:
    def __init__(self, fail: bool):
        self.fail = fail

    async def __aenter__(self) -> "FakeConn":
        if self.fail:
            raise ConnectionError("db down")
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def execute(self, query: object) -> None:
        return None


class FakeEngine:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.disposed = False

    def connect(self) -> FakeConn:
        return FakeConn(self.fail)

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.parametrize("db_fails", [False, True])
async def test_worker_heartbeats_then_disposes_engine(
    monkeypatch: pytest.MonkeyPatch, db_fails: bool
) -> None:
    engine = FakeEngine(fail=db_fails)
    monkeypatch.setattr(worker, "create_async_engine", lambda url: engine)

    async def cancel_after_first(seconds: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker.asyncio, "sleep", cancel_after_first)

    with pytest.raises(asyncio.CancelledError):
        await worker.run()

    assert engine.disposed
