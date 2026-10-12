"""Migration 0231 against real Postgres: who may ask a panel, and who may answer.

Three principals, as the box runs them: the OWNER (the debug console's context), the panel
a request is FOR, and a SIBLING panel on the next wall. The owner queues and reads
everything. The panel reads only its own live requests and writes only their results. The
sibling can neither see, collect, nor answer the first panel's requests — a panel that
could answer its sibling's would hand the owner a forged screenshot of the other room —
and no panel can create a request at all.

The trigger half is asserted the same way: RLS chooses rows, not columns, so it is the
trigger that keeps a panel from rewriting the command it was sent or holding a request
open past its deadline.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

PANEL = uuid.uuid4()
SIBLING = uuid.uuid4()
# Exactly as `ctx_for` builds a flashed panel's context: its id, `device_key`, nothing else.
PANEL_CTX = SessionContext(principal_id=str(PANEL), principal_kind="device_key")
SIBLING_CTX = SessionContext(principal_id=str(SIBLING), principal_kind="device_key")
# A capability-token context that is not the owner: it must see nothing.
STRANGER_CTX = SessionContext(principal_id=str(uuid.uuid4()), principal_kind="capability")

Maker = async_sessionmaker[AsyncSession]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.panel_debug_request"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        for pid, label in ((PANEL, "panel Ellie"), (SIBLING, "panel Nora")):
            await s.execute(
                text(
                    "INSERT INTO app.principals (id, kind, key_hash, label)"
                    " VALUES (:id, 'device_key', :hash, :label)"
                ),
                {"id": pid, "hash": f"hash-{pid}", "label": label},
            )
        await s.commit()
    yield sessions
    await engine.dispose()


async def _queue(maker: Maker, device: uuid.UUID, command: str = "snapshot") -> str:
    async with scoped_session(maker, OWNER) as s:
        rid = (
            await s.execute(
                text(
                    """
                    INSERT INTO app.panel_debug_request
                        (device_id, command, created_by, expires_at)
                    VALUES (:dev, :cmd, 'test token', now() + interval '60 seconds')
                    RETURNING id::text
                    """
                ),
                {"dev": device, "cmd": command},
            )
        ).scalar_one()
        await s.commit()
    return str(rid)


async def _visible(maker: Maker, ctx: SessionContext) -> set[str]:
    async with scoped_session(maker, ctx) as s:
        return {
            str(r[0])
            for r in (await s.execute(text("SELECT id::text FROM app.panel_debug_request"))).all()
        }


async def _update(maker: Maker, ctx: SessionContext, rid: str, sets: str) -> int:
    """Run `UPDATE … SET <sets> WHERE id = rid` as `ctx` and return how many rows changed."""
    async with scoped_session(maker, ctx) as s:
        n = (
            await s.execute(
                text(
                    f"UPDATE app.panel_debug_request SET {sets}"
                    " WHERE id = CAST(:id AS uuid) RETURNING id"
                ),
                {"id": rid},
            )
        ).all()
        await s.commit()
    return len(n)


async def _row(maker: Maker, rid: str) -> dict:
    async with scoped_session(maker, OWNER) as s:
        row = (
            (
                await s.execute(
                    text("SELECT * FROM app.panel_debug_request WHERE id = CAST(:id AS uuid)"),
                    {"id": rid},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


# ---- the owner -------------------------------------------------------------------------


async def test_the_owner_queues_and_reads_every_panel_s_requests(maker: Maker) -> None:
    mine = await _queue(maker, PANEL)
    theirs = await _queue(maker, SIBLING)
    assert await _visible(maker, OWNER) == {mine, theirs}


async def test_a_non_owner_non_panel_context_sees_nothing(maker: Maker) -> None:
    await _queue(maker, PANEL)
    assert await _visible(maker, STRANGER_CTX) == set()
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, STRANGER_CTX) as s:
            await s.execute(
                text(
                    "INSERT INTO app.panel_debug_request (device_id, command, created_by,"
                    " expires_at) VALUES (:dev, 'reboot', 'x', now() + interval '1 hour')"
                ),
                {"dev": PANEL},
            )


# ---- the panel itself ------------------------------------------------------------------


async def test_a_panel_sees_only_its_own_live_requests(maker: Maker) -> None:
    mine = await _queue(maker, PANEL)
    await _queue(maker, SIBLING)
    assert await _visible(maker, PANEL_CTX) == {mine}


async def test_a_panel_collects_and_answers_its_own_request(maker: Maker) -> None:
    rid = await _queue(maker, PANEL)
    assert await _update(maker, PANEL_CTX, rid, "state = 'sent'") == 1
    sent = await _row(maker, rid)
    assert sent["state"] == "sent" and sent["picked_up_at"] is not None
    assert await _update(maker, PANEL_CTX, rid, """state = 'done', result = '{"ok": true}'""") == 1
    done = await _row(maker, rid)
    assert done["state"] == "done" and done["result"] == {"ok": True}
    assert done["result_at"] is not None
    # Done is the end: the panel no longer sees it, so cannot answer twice.
    assert await _visible(maker, PANEL_CTX) == set()
    assert await _update(maker, PANEL_CTX, rid, """result = '{"ok": false}'""") == 0
    assert (await _row(maker, rid))["result"] == {"ok": True}


async def test_a_panel_cannot_create_a_request_even_for_itself(maker: Maker) -> None:
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, PANEL_CTX) as s:
            await s.execute(
                text(
                    "INSERT INTO app.panel_debug_request (device_id, command, created_by,"
                    " expires_at) VALUES (:dev, 'snapshot', 'me', now() + interval '1 hour')"
                ),
                {"dev": PANEL},
            )


async def test_a_panel_cannot_delete_its_requests(maker: Maker) -> None:
    rid = await _queue(maker, PANEL)
    async with scoped_session(maker, PANEL_CTX) as s:
        gone = len(
            (
                await s.execute(
                    text(
                        "DELETE FROM app.panel_debug_request WHERE id = CAST(:id AS uuid)"
                        " RETURNING id"
                    ),
                    {"id": rid},
                )
            ).all()
        )
        await s.commit()
    assert gone == 0
    assert (await _row(maker, rid))["state"] == "queued"


@pytest.mark.parametrize(
    "sets",
    [
        "command = 'reboot'",
        """args = '{"x": 1}'""",
        "confirm = true",
        "created_by = 'someone else'",
        "created_at = now() - interval '1 day'",
    ],
)
async def test_a_panel_cannot_rewrite_the_request_it_was_sent(maker: Maker, sets: str) -> None:
    rid = await _queue(maker, PANEL)
    with pytest.raises(DBAPIError, match="may not change the request"):
        await _update(maker, PANEL_CTX, rid, f"state = 'sent', {sets}")


async def test_a_panel_cannot_hand_its_request_to_its_sibling(maker: Maker) -> None:
    rid = await _queue(maker, PANEL)
    with pytest.raises(DBAPIError):
        await _update(maker, PANEL_CTX, rid, f"state = 'sent', device_id = '{SIBLING}'")


@pytest.mark.parametrize(
    ("first", "then"),
    [
        (None, "state = 'done'"),  # answering what it never collected
        (None, "state = 'expired'"),
        ("state = 'sent'", "state = 'queued'"),  # un-collecting
        ("state = 'sent'", "state = 'sent'"),  # re-collecting to reset the deadline
    ],
)
async def test_only_collect_then_answer(maker: Maker, first: str | None, then: str) -> None:
    rid = await _queue(maker, PANEL)
    if first:
        assert await _update(maker, PANEL_CTX, rid, first) == 1
    with pytest.raises(DBAPIError):
        await _update(maker, PANEL_CTX, rid, then)


async def test_the_deadline_is_the_box_s_not_the_panel_s(maker: Maker) -> None:
    """On pickup the trigger sets the answer window; a far-future `expires_at` from the
    panel is overwritten, and on answer the panel cannot move it either."""
    rid = await _queue(maker, PANEL)
    await _update(maker, PANEL_CTX, rid, "state = 'sent', expires_at = now() + interval '1 year'")
    sent = await _row(maker, rid)
    window = (sent["expires_at"] - sent["picked_up_at"]).total_seconds()
    assert window == pytest.approx(120, abs=1)
    await _update(
        maker,
        PANEL_CTX,
        rid,
        "state = 'done', expires_at = now() + interval '1 year',"
        " picked_up_at = now() - interval '1 day'",
    )
    done = await _row(maker, rid)
    assert done["expires_at"] == sent["expires_at"]
    assert done["picked_up_at"] == sent["picked_up_at"]


async def test_an_expired_request_is_invisible_and_unanswerable(maker: Maker) -> None:
    rid = await _queue(maker, PANEL)
    await _update(maker, PANEL_CTX, rid, "state = 'sent'")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.panel_debug_request SET expires_at = now() - interval '1 second'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": rid},
        )
        await s.commit()
    assert await _visible(maker, PANEL_CTX) == set()
    assert await _update(maker, PANEL_CTX, rid, "state = 'done'") == 0
    assert (await _row(maker, rid))["state"] == "sent"


async def test_the_owner_is_not_bound_by_the_panel_s_trigger(maker: Maker) -> None:
    """The sweep marks rows expired and the owner may correct a row; the trigger holds
    only a `device_key` caller."""
    rid = await _queue(maker, PANEL)
    assert await _update(maker, OWNER, rid, "state = 'expired', command = 'logs'") == 1


# ---- the sibling -----------------------------------------------------------------------


async def test_a_sibling_cannot_see_collect_or_answer_another_panel_s_request(
    maker: Maker,
) -> None:
    rid = await _queue(maker, PANEL)
    assert await _visible(maker, SIBLING_CTX) == set()
    # Collect: RLS hides the row, so the update matches nothing.
    assert await _update(maker, SIBLING_CTX, rid, "state = 'sent'") == 0
    assert (await _row(maker, rid))["state"] == "queued"
    # Answer, after the real panel collected it.
    await _update(maker, PANEL_CTX, rid, "state = 'sent'")
    forged = """state = 'done', result = '{"ok": true, "data": {"forged": 1}}'"""
    assert await _update(maker, SIBLING_CTX, rid, forged) == 0
    row = await _row(maker, rid)
    assert row["state"] == "sent" and row["result"] is None


async def test_a_sibling_cannot_create_a_request_for_another_panel(maker: Maker) -> None:
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, SIBLING_CTX) as s:
            await s.execute(
                text(
                    "INSERT INTO app.panel_debug_request (device_id, command, created_by,"
                    " expires_at) VALUES (:dev, 'reboot', 'x', now() + interval '1 hour')"
                ),
                {"dev": PANEL},
            )


# ---- shape -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("command", "'Reboot!'"),
        ("args", "'[1, 2]'"),
        ("args", f'\'{{"app": "{"x" * 600}"}}\''),
        ("png_sha256", "'not-a-digest'"),
        ("state", "'lost'"),
    ],
)
async def test_the_table_refuses_malformed_rows(maker: Maker, column: str, value: str) -> None:
    rid = await _queue(maker, PANEL)
    with pytest.raises(DBAPIError):
        await _update(maker, OWNER, rid, f"{column} = {value}")


async def test_revoking_a_panel_by_deleting_its_key_takes_its_requests(maker: Maker) -> None:
    rid = await _queue(maker, PANEL)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.principals WHERE id = :id"), {"id": PANEL})
        await s.commit()
    assert rid not in await _visible(maker, OWNER)
