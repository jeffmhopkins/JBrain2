"""Migration 0235 against real Postgres: who may send a notice, and who may answer it.

Three principals, as the box runs them: the OWNER (every box-side sender runs as the owner),
the panel a notice is FOR, and a SIBLING panel. The owner sends and reads everything. The
panel reads only its own live notices and may only move them along, recording which of the
offered answers was tapped. The sibling can neither see, collect nor answer the first panel's
notices — a panel that could answer its sibling's question would answer for the owner — and no
panel can create or delete one.

RLS chooses rows, not columns, so the trigger half is asserted the same way: a panel cannot
rewrite what a notice says, invent an answer, or hold a question open past its deadline.
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

from jbrain.blob_refs import blob_referenced
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

PANEL = uuid.uuid4()
SIBLING = uuid.uuid4()
PANEL_CTX = SessionContext(principal_id=str(PANEL), principal_kind="device_key")
SIBLING_CTX = SessionContext(principal_id=str(SIBLING), principal_kind="device_key")
STRANGER_CTX = SessionContext(principal_id=str(uuid.uuid4()), principal_kind="capability")

Maker = async_sessionmaker[AsyncSession]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.panel_notice"))
        await s.execute(text("DELETE FROM app.panel_debug_request"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        for pid, label in ((PANEL, "panel Jeff"), (SIBLING, "panel Quorra")):
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


_INSERT = """
    INSERT INTO app.panel_notice
        (device_id, sender, title, body, choices, ttl_s, created_by, expires_at)
    VALUES (:dev, 'jerv', 'Run the cleanup now?', 'Frees 38 GB.', CAST(:choices AS jsonb),
            900, 'test', now() + interval '60 seconds')
    RETURNING id::text
"""


async def _send(maker: Maker, device: uuid.UUID, choices: str = '["Yes", "No"]') -> str:
    async with scoped_session(maker, OWNER) as s:
        nid = (await s.execute(text(_INSERT), {"dev": device, "choices": choices})).scalar_one()
        await s.commit()
    return str(nid)


async def _visible(maker: Maker, ctx: SessionContext) -> set[str]:
    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(text("SELECT id::text FROM app.panel_notice"))).all()
    return {str(r[0]) for r in rows}


async def _update(maker: Maker, ctx: SessionContext, nid: str, sets: str) -> int:
    async with scoped_session(maker, ctx) as s:
        n = (
            await s.execute(
                text(
                    f"UPDATE app.panel_notice SET {sets} WHERE id = CAST(:id AS uuid) RETURNING id"
                ),
                {"id": nid},
            )
        ).all()
        await s.commit()
    return len(n)


async def _row(maker: Maker, nid: str) -> dict:
    async with scoped_session(maker, OWNER) as s:
        row = (
            (
                await s.execute(
                    text("SELECT * FROM app.panel_notice WHERE id = CAST(:id AS uuid)"),
                    {"id": nid},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


# ---- the owner -------------------------------------------------------------------------


async def test_the_owner_sends_and_reads_every_panel_s_notices(maker: Maker) -> None:
    mine = await _send(maker, PANEL)
    theirs = await _send(maker, SIBLING)
    assert await _visible(maker, OWNER) == {mine, theirs}


async def test_a_non_owner_non_panel_context_sees_and_sends_nothing(maker: Maker) -> None:
    await _send(maker, PANEL)
    assert await _visible(maker, STRANGER_CTX) == set()
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, STRANGER_CTX) as s:
            await s.execute(text(_INSERT), {"dev": PANEL, "choices": "[]"})


async def test_the_owner_is_not_bound_by_the_panel_s_trigger(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    assert await _update(maker, OWNER, nid, "state = 'expired', title = 'Swept'") == 1


# ---- the panel itself ------------------------------------------------------------------


async def test_a_panel_sees_only_its_own_live_notices(maker: Maker) -> None:
    mine = await _send(maker, PANEL)
    await _send(maker, SIBLING)
    assert await _visible(maker, PANEL_CTX) == {mine}


async def test_a_panel_collects_shows_and_answers_its_own_notice(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    assert await _update(maker, PANEL_CTX, nid, "state = 'delivered'") == 1
    row = await _row(maker, nid)
    assert row["state"] == "delivered" and row["delivered_at"] is not None
    # Delivery starts the answer window: the notice's own ttl, from now.
    window = (row["expires_at"] - row["delivered_at"]).total_seconds()
    assert window == pytest.approx(900, abs=1)
    assert await _update(maker, PANEL_CTX, nid, "state = 'shown'") == 1
    assert (await _row(maker, nid))["shown_at"] is not None
    assert await _update(maker, PANEL_CTX, nid, "state = 'answered', answer = 'No'") == 1
    done = await _row(maker, nid)
    assert (done["state"], done["answer"]) == ("answered", "No")
    assert done["finished_at"] is not None
    # Answered is the end: the panel no longer sees it, so it cannot answer twice.
    assert await _visible(maker, PANEL_CTX) == set()
    assert await _update(maker, PANEL_CTX, nid, "answer = 'Yes'") == 0
    assert (await _row(maker, nid))["answer"] == "No"


@pytest.mark.parametrize("end", ["dismissed", "expired"])
async def test_a_panel_hides_a_notice_without_an_answer(maker: Maker, end: str) -> None:
    nid = await _send(maker, PANEL, "[]")
    await _update(maker, PANEL_CTX, nid, "state = 'delivered'")
    assert await _update(maker, PANEL_CTX, nid, f"state = '{end}'") == 1
    row = await _row(maker, nid)
    assert row["state"] == end and row["answer"] is None and row["finished_at"] is not None


async def test_an_answer_straight_from_delivered_stamps_shown(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    await _update(maker, PANEL_CTX, nid, "state = 'delivered'")
    assert await _update(maker, PANEL_CTX, nid, "state = 'answered', answer = 'Yes'") == 1
    assert (await _row(maker, nid))["shown_at"] is not None


@pytest.mark.parametrize("answer", ["'Maybe'", "'yes'", "NULL", "'Yes '"])
async def test_an_answer_must_be_one_of_the_notice_s_choices(maker: Maker, answer: str) -> None:
    nid = await _send(maker, PANEL)
    await _update(maker, PANEL_CTX, nid, "state = 'delivered'")
    with pytest.raises(DBAPIError):
        await _update(maker, PANEL_CTX, nid, f"state = 'answered', answer = {answer}")
    assert (await _row(maker, nid))["state"] == "delivered"


async def test_a_plain_notice_cannot_be_answered(maker: Maker) -> None:
    nid = await _send(maker, PANEL, "[]")
    await _update(maker, PANEL_CTX, nid, "state = 'delivered'")
    with pytest.raises(DBAPIError, match="one of the notice"):
        await _update(maker, PANEL_CTX, nid, "state = 'answered', answer = 'OK'")


async def test_a_panel_cannot_create_a_notice_even_for_itself(maker: Maker) -> None:
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, PANEL_CTX) as s:
            await s.execute(text(_INSERT), {"dev": PANEL, "choices": "[]"})


async def test_a_panel_cannot_delete_its_notices(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    async with scoped_session(maker, PANEL_CTX) as s:
        gone = (
            await s.execute(
                text("DELETE FROM app.panel_notice WHERE id = CAST(:id AS uuid) RETURNING id"),
                {"id": nid},
            )
        ).all()
        await s.commit()
    assert gone == []
    assert (await _row(maker, nid))["state"] == "queued"


@pytest.mark.parametrize(
    "sets",
    [
        "title = 'Forged'",
        "body = ''",
        """choices = '["Yes"]'""",
        "chime = true",
        "hide_after_s = 5",
        "ttl_s = 86400",
        "sender = 'ops'",
        "image_mode = 'full'",
        "audio_ms = 10",
        "created_by = 'someone else'",
        "created_at = now() - interval '1 day'",
    ],
)
async def test_a_panel_cannot_rewrite_the_notice_it_was_sent(maker: Maker, sets: str) -> None:
    nid = await _send(maker, PANEL)
    with pytest.raises(DBAPIError, match="may not change the notice"):
        await _update(maker, PANEL_CTX, nid, f"state = 'delivered', {sets}")


async def test_a_panel_cannot_hand_its_notice_to_its_sibling(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    with pytest.raises(DBAPIError):
        await _update(maker, PANEL_CTX, nid, f"state = 'delivered', device_id = '{SIBLING}'")


@pytest.mark.parametrize(
    ("first", "then"),
    [
        ([], "state = 'shown'"),  # showing what it never collected
        ([], "state = 'answered', answer = 'Yes'"),
        ([], "state = 'dismissed'"),
        (["state = 'delivered'"], "state = 'queued'"),  # un-collecting
        (["state = 'delivered'"], "state = 'delivered'"),  # re-collecting to reset the window
        (["state = 'delivered'", "state = 'shown'"], "state = 'delivered'"),
        (["state = 'delivered'", "state = 'shown'"], "state = 'shown'"),
    ],
)
async def test_only_forward_moves(maker: Maker, first: list[str], then: str) -> None:
    nid = await _send(maker, PANEL)
    for step in first:
        assert await _update(maker, PANEL_CTX, nid, step) == 1
    with pytest.raises(DBAPIError):
        await _update(maker, PANEL_CTX, nid, then)


async def test_the_clock_is_the_box_s_not_the_panel_s(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    await _update(
        maker,
        PANEL_CTX,
        nid,
        "state = 'delivered', expires_at = now() + interval '1 year',"
        " delivered_at = now() - interval '1 day', finished_at = now()",
    )
    row = await _row(maker, nid)
    assert (row["expires_at"] - row["delivered_at"]).total_seconds() == pytest.approx(900, abs=1)
    assert row["finished_at"] is None
    await _update(maker, PANEL_CTX, nid, "state = 'shown', shown_at = now() - interval '1 day'")
    shown = await _row(maker, nid)
    assert (shown["shown_at"] - row["delivered_at"]).total_seconds() >= 0
    assert shown["expires_at"] == row["expires_at"]


async def test_an_expired_notice_is_invisible_and_unanswerable(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    await _update(maker, PANEL_CTX, nid, "state = 'delivered'")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.panel_notice SET expires_at = now() - interval '1 second'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": nid},
        )
        await s.commit()
    assert await _visible(maker, PANEL_CTX) == set()
    assert await _update(maker, PANEL_CTX, nid, "state = 'answered', answer = 'Yes'") == 0
    assert (await _row(maker, nid))["state"] == "delivered"


# ---- withdrawn (migration 0249) -------------------------------------------------------------


async def _withdraw(maker: Maker, nid: str) -> int:
    return await _update(maker, OWNER, nid, "state = 'withdrawn', finished_at = now()")


async def test_a_panel_sees_a_withdrawn_notice_it_collected_while_it_could_hold_it(
    maker: Maker,
) -> None:
    held = await _send(maker, PANEL)
    await _update(maker, PANEL_CTX, held, "state = 'delivered'")
    never = await _send(maker, PANEL)
    assert await _withdraw(maker, held) == 1
    assert await _withdraw(maker, never) == 1
    assert await _visible(maker, PANEL_CTX) == {held}, "only one it collected"
    assert await _visible(maker, SIBLING_CTX) == set()
    await _update(maker, OWNER, held, "expires_at = now() - interval '1 second'")
    assert await _visible(maker, PANEL_CTX) == set(), "past its window it is gone again"


async def test_a_panel_cannot_move_a_withdrawn_notice(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    await _update(maker, PANEL_CTX, nid, "state = 'delivered'")
    await _withdraw(maker, nid)
    for sets in ("state = 'shown'", "state = 'answered', answer = 'Yes'", "state = 'dismissed'"):
        assert await _update(maker, PANEL_CTX, nid, sets) == 0
    assert (await _row(maker, nid))["state"] == "withdrawn"


async def test_withdrawn_is_a_state_the_table_takes(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    assert await _withdraw(maker, nid) == 1
    with pytest.raises(DBAPIError):
        await _update(maker, OWNER, nid, "state = 'recalled'")


# ---- the sibling -----------------------------------------------------------------------


async def test_a_sibling_cannot_see_collect_or_answer_another_panel_s_notice(
    maker: Maker,
) -> None:
    nid = await _send(maker, PANEL)
    assert await _visible(maker, SIBLING_CTX) == set()
    assert await _update(maker, SIBLING_CTX, nid, "state = 'delivered'") == 0
    assert (await _row(maker, nid))["state"] == "queued"
    await _update(maker, PANEL_CTX, nid, "state = 'delivered'")
    assert await _update(maker, SIBLING_CTX, nid, "state = 'answered', answer = 'Yes'") == 0
    row = await _row(maker, nid)
    assert row["state"] == "delivered" and row["answer"] is None


async def test_a_sibling_cannot_send_to_another_panel(maker: Maker) -> None:
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, SIBLING_CTX) as s:
            await s.execute(text(_INSERT), {"dev": PANEL, "choices": "[]"})


# ---- shape -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sender", "'Jerv'"),
        ("sender", "'workflow:'"),
        ("title", "''"),
        ("title", f"'{'x' * 61}'"),
        ("body", f"'{'x' * 301}'"),
        ("choices", """'["a", "b", "c", "d", "e"]'"""),
        ("choices", """'["seventeen bytes!!"]'"""),
        ("choices", """'[""]'"""),
        ("choices", """'[1]'"""),
        ("choices", """'{"a": 1}'"""),
        ("hide_after_s", "2"),
        ("hide_after_s", "601"),
        ("ttl_s", "29"),
        ("image_sha256", "'not-a-digest'"),
        ("audio_ms", "8001"),
        ("state", "'lost'"),
        ("answer", "'Yes'"),  # an answer on a notice that is not answered
    ],
)
async def test_the_table_refuses_malformed_rows(maker: Maker, column: str, value: str) -> None:
    nid = await _send(maker, PANEL)
    with pytest.raises(DBAPIError):
        await _update(maker, OWNER, nid, f"{column} = {value}")


async def test_a_picture_and_a_sound_come_whole_or_not_at_all(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    with pytest.raises(DBAPIError):
        await _update(maker, OWNER, nid, f"image_sha256 = '{'a' * 64}'")
    with pytest.raises(DBAPIError):
        await _update(maker, OWNER, nid, f"audio_sha256 = '{'b' * 64}'")
    sets = (
        f"image_sha256 = '{'a' * 64}', image_mode = 'banner', image_w = 480, image_h = 64,"
        f" image_bytes = 900, audio_sha256 = '{'b' * 64}', audio_ms = 2500"
    )
    assert await _update(maker, OWNER, nid, sets) == 1


async def test_the_picture_and_sound_are_registered_blob_references(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    sets = (
        f"image_sha256 = '{'c' * 64}', image_mode = 'full', image_w = 480, image_h = 480,"
        f" image_bytes = 900, audio_sha256 = '{'d' * 64}', audio_ms = 100"
    )
    await _update(maker, OWNER, nid, sets)
    async with scoped_session(maker, OWNER) as s:
        assert await blob_referenced(s, "c" * 64)
        assert await blob_referenced(s, "d" * 64)
        assert not await blob_referenced(s, "e" * 64)


async def test_revoking_a_panel_by_deleting_its_key_takes_its_notices(maker: Maker) -> None:
    nid = await _send(maker, PANEL)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.principals WHERE id = :id"), {"id": PANEL})
        await s.commit()
    assert nid not in await _visible(maker, OWNER)
