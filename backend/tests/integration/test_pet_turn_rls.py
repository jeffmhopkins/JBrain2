"""Migration 0219 against real Postgres: a panel writes what it heard and can never read it back.

THE POINT OF THIS FILE IS THE MISSING SELECT POLICY. `pet_turn` holds transcripts of small
children talking to a toy, written by a device that hangs on a bedroom wall and authenticates
with a key a four-year-old could hand to a visitor. `0208_jpanel_message` had to let a panel read
rows addressed to it, because delivering a message requires reading it. Nothing here needs
reading back: the pet's own memory of a conversation is four minutes of process RAM in the API
(`_panel_memory`), deliberately not this table. So the panel has INSERT and nothing else.

An absent policy is the easiest thing in a migration to add by accident later — "the panel needs
to see its own turns" is a plausible-sounding sentence that would quietly turn a diary into
something readable from a bedroom wall. Asserted in Postgres under the exact context a flashed
panel runs as, because "the route has no such handler" is a code-review convention rather than a
mechanism (CLAUDE.md rule 3, and `0178_settings_deny_jmolt` for the argument).
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The two panels, exactly as a flashed unit runs: a device key, no domain scopes, not owner.
ONE = SessionContext(principal_id="panel-one", principal_kind="device_key")
TWO = SessionContext(principal_id="panel-two", principal_kind="device_key")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _say(maker: async_sessionmaker, ctx: SessionContext, dev: str, heard: str) -> None:
    """A panel filing a turn, through its own scoped session — the route's exact path."""
    async with scoped_session(maker, ctx) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.pet_turn (device_id, heard, reply)
                VALUES (:dev, :heard, 'the pet said something')
                """
            ),
            {"dev": dev, "heard": heard},
        )
        await s.commit()


async def test_a_panel_can_write_what_it_heard(maker: async_sessionmaker) -> None:
    """The write has to work under the panel's own context or the route cannot log anything —
    `/endpoint/converse` authenticates as the panel and inserts without escalating."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.pet_turn"))
        await s.commit()

    await _say(maker, ONE, "panel-one", "why is the sky blue")

    async with scoped_session(maker, OWNER) as s:
        got = (await s.execute(text("SELECT heard FROM app.pet_turn"))).scalar_one()
    assert got == "why is the sky blue", "the panel's own turn never reached the table"


async def test_a_panel_cannot_read_back_even_its_own_conversations(
    maker: async_sessionmaker,
) -> None:
    """THE WHOLE FILE. There is no panel SELECT policy and there must not be one.

    A panel that can read this table can recite what a child told it in private, to anyone who
    can reach the panel — and unlike a message, nothing about the feature needs that. The pet
    remembers a conversation for four minutes in the API's own memory; the panel never asks."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.pet_turn"))
        await s.commit()
    await _say(maker, ONE, "panel-one", "a secret")

    async with scoped_session(maker, ONE) as s:
        mine = (await s.execute(text("SELECT count(*) FROM app.pet_turn"))).scalar_one()
    assert mine == 0, (
        "a panel can read the transcript of what a child said to it — this table has acquired a "
        "panel SELECT policy, and a device on a bedroom wall can now recite private conversations"
    )


async def test_a_panel_cannot_read_its_siblings_conversations(maker: async_sessionmaker) -> None:
    """The sibling case, asserted separately from the self case above because they fail
    separately: a policy scoped to `device_id = principal_id` would pass the one below and break
    the one above, and that policy is exactly what a well-meaning later change would add."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.pet_turn"))
        await s.commit()
    await _say(maker, ONE, "panel-one", "something she told it")

    async with scoped_session(maker, TWO) as s:
        seen = (await s.execute(text("SELECT count(*) FROM app.pet_turn"))).scalar_one()
    assert seen == 0, "one twin can read the other's conversations with the pet"


async def test_a_panel_cannot_file_words_under_its_siblings_name(
    maker: async_sessionmaker,
) -> None:
    """The anti-impersonation bound, and on THIS table it is not a forged message but a forged
    account of what a child said. `0208`'s argument, one table along."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.pet_turn"))
        await s.commit()

    with pytest.raises(Exception):  # noqa: B017 — the policy's refusal, whatever its class
        await _say(maker, ONE, "panel-two", "words her sister never said")

    async with scoped_session(maker, OWNER) as s:
        left = (await s.execute(text("SELECT count(*) FROM app.pet_turn"))).scalar_one()
    assert left == 0, "a panel filed a conversation under its sibling's name"


async def test_a_panel_cannot_rewrite_or_delete_what_it_said(maker: async_sessionmaker) -> None:
    """INSERT AND NOTHING ELSE. A record a panel could edit is not a record — and the erase is
    the one that matters: a child who knew the pet tells on her would have every reason to find
    out whether the pet can be made to forget."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.pet_turn"))
        await s.commit()
    await _say(maker, ONE, "panel-one", "the truth")

    async with scoped_session(maker, ONE) as s:
        await s.execute(text("UPDATE app.pet_turn SET heard = 'something else'"))
        await s.execute(text("DELETE FROM app.pet_turn"))
        await s.commit()

    async with scoped_session(maker, OWNER) as s:
        rows = (await s.execute(text("SELECT heard FROM app.pet_turn"))).all()
    assert len(rows) == 1 and rows[0][0] == "the truth", (
        "a panel rewrote or deleted a conversation it had already filed"
    )


async def test_the_owner_sees_every_panel(maker: async_sessionmaker) -> None:
    """The other half: the read this table exists for has to actually work, across panels, which
    is what makes the tab's grouping possible."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.pet_turn"))
        await s.commit()
    await _say(maker, ONE, "panel-one", "from the first twin")
    await _say(maker, TWO, "panel-two", "from the second twin")

    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(text("SELECT device_id, heard FROM app.pet_turn ORDER BY device_id"))
        ).all()
    assert [(r[0], r[1]) for r in rows] == [
        ("panel-one", "from the first twin"),
        ("panel-two", "from the second twin"),
    ]
