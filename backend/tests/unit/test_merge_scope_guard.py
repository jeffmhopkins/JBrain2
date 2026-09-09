"""The fold's scope guard, at the seam: it asks Postgres whether the session is
narrowed and refuses BEFORE issuing a single write, so a refused merge cannot leave
a half-repointed graph. The cross-domain behaviour itself is proven against real
Postgres in tests/integration/test_merge_scoping_pg.py."""

import uuid
from contextlib import contextmanager
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.analysis.entities import MergeScopeError, merge_entity_pair


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value

    def scalars(self) -> list[str]:
        return []


class _FakeSession:
    """Records every statement, and answers `app.is_full_owner()` with `full_owner`.

    `no_autoflush` is modelled because the guard runs its probe inside it — a real
    `AsyncSession.execute` would otherwise flush a caller's pending ORM state, and
    "refuses before writing anything" would quietly stop being true.
    """

    def __init__(self, *, full_owner: bool) -> None:
        self.full_owner = full_owner
        self.statements: list[str] = []
        self.autoflush_suppressed: list[bool] = []
        self._suppressed = False

    @property
    @contextmanager
    def no_autoflush(self) -> Any:
        self._suppressed = True
        try:
            yield self
        finally:
            self._suppressed = False

    async def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        self.autoflush_suppressed.append(self._suppressed)
        if "is_full_owner" in sql:
            return _Result(self.full_owner)
        return _Result(None)


def _writes(session: _FakeSession) -> list[str]:
    return [s for s in session.statements if s.lstrip().upper().startswith("UPDATE")]


async def test_narrowed_session_refuses_before_any_write() -> None:
    session = _FakeSession(full_owner=False)
    with pytest.raises(MergeScopeError) as caught:
        await merge_entity_pair(cast(AsyncSession, session), keep=uuid.uuid4(), gone=uuid.uuid4())
    # No write of any kind ran — in particular the tombstone UPDATE never fired,
    # which is what makes the refusal free of partial effect.
    assert _writes(session) == []
    assert "full-owner session" in str(caught.value)


async def test_the_probe_does_not_flush_a_callers_pending_state() -> None:
    """The probe must not be the statement that flushes pending ORM writes — a
    refusal that first commits half of someone else's work is not a refusal."""
    session = _FakeSession(full_owner=False)
    with pytest.raises(MergeScopeError):
        await merge_entity_pair(cast(AsyncSession, session), keep=uuid.uuid4(), gone=uuid.uuid4())
    assert session.autoflush_suppressed == [True]


async def test_full_owner_session_runs_the_whole_fold() -> None:
    session = _FakeSession(full_owner=True)
    repointed = await merge_entity_pair(
        cast(AsyncSession, session), keep=uuid.uuid4(), gone=uuid.uuid4()
    )
    assert set(repointed) == {"mention_ids", "fact_ids", "object_fact_ids"}
    # The probe leads, then the tombstone, then the three repoints. Asserted by shape
    # rather than by a total count, so adding a statement to the fold doesn't fail
    # this test for the wrong reason.
    assert "is_full_owner" in session.statements[0]
    writes = _writes(session)
    assert len(writes) == 4 and "UPDATE app.entities" in writes[0]
