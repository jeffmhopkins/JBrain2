"""Migration 0275 against real Postgres: `model_calls` is owner-only (CLAUDE.md rule 3), and the
window read the vitals page polls holds: a call is listed while it runs, grouped by who asked,
a creature named by its persona, and a row a dead process left open reads as stale."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain import model_calls
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

OWNER = SessionContext(principal_kind="owner")
NON_OWNER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def wired(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(maker, OWNER) as session:
        await session.execute(text("DELETE FROM app.model_calls"))
    model_calls.configure(maker, source="api")
    yield maker
    model_calls.reset()
    await engine.dispose()


async def _settled() -> None:
    await asyncio.gather(*model_calls._TASKS)  # noqa: SLF001 - wait for the narration to land


async def _until_open(maker: async_sessionmaker) -> None:
    for _ in range(100):
        async with scoped_session(maker, OWNER) as session:
            if (await session.execute(text("SELECT count(*) FROM app.model_calls"))).scalar():
                return
        await asyncio.sleep(0.01)


async def test_a_call_is_listed_while_it_runs_and_grouped_after(
    wired: async_sessionmaker,
) -> None:
    async with scoped_session(wired, OWNER) as session:
        persona = (
            await session.execute(
                text(
                    "INSERT INTO app.growbot_personas (name, soul) VALUES ('Dave', '{}')"
                    " RETURNING id"
                )
            )
        ).scalar()

    async with model_calls.track(
        model_calls.CREATURE, "think", model="qwen3-vl", persona_id=str(persona), robot="gb-a"
    ):
        await _until_open(wired)
        during = await model_calls.window(wired, OWNER, seconds=60)
        assert [(c["who"], c["label"], c["status"]) for c in during["calls"]] == [
            ("Dave", "think", "running")
        ]
        assert during["groups"][0]["running"] == 1
    await _settled()
    async with model_calls.track(model_calls.CREATURE, "think", model="qwen3-vl", robot="gb-a"):
        pass
    async with model_calls.track(
        model_calls.BACKGROUND, "reading text in an image", model="grok-4.3", local=False
    ):
        pass
    await _settled()

    found = await model_calls.window(wired, OWNER, seconds=60)
    assert [c["status"] for c in found["calls"]] == ["ok", "ok", "ok"]
    groups = {(g["caller"], g["who"]): g for g in found["groups"]}
    # No persona on the second think: it is named by its robot instead.
    assert groups[("creature", "Dave")]["calls"] == 1
    assert groups[("creature", "gb-a")]["calls"] == 1
    assert groups[("background", "")]["local"] is False
    assert all(g["running"] == 0 for g in found["groups"])


async def test_a_row_a_dead_process_left_open_reads_stale(wired: async_sessionmaker) -> None:
    abandoned = datetime.now(tz=UTC) - model_calls.STALE_AFTER - timedelta(minutes=1)
    async with scoped_session(wired, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.model_calls (started_at, caller, model) "
                "VALUES (:at, 'assistant', 'm')"
            ),
            {"at": abandoned},
        )
    wide = await model_calls.window(wired, OWNER, seconds=3600)
    assert [c["status"] for c in wide["calls"]] == ["stale"]
    assert wide["groups"][0]["running"] == 0
    assert (await model_calls.window(wired, OWNER, seconds=60))["calls"] == []


async def test_prune_drops_rows_past_retention(wired: async_sessionmaker) -> None:
    old = datetime.now(tz=UTC) - model_calls.RETENTION - timedelta(hours=1)
    async with scoped_session(wired, OWNER) as session:
        await session.execute(
            text(
                "INSERT INTO app.model_calls (started_at, ended_at, caller, model) "
                "VALUES (:at, :at, 'assistant', 'ancient')"
            ),
            {"at": old},
        )
    async with model_calls.track(model_calls.ASSISTANT, "x", model="recent"):
        pass
    await _settled()
    assert await model_calls.prune(wired, OWNER) == 1


async def test_non_owner_sees_nothing_and_cannot_write(wired: async_sessionmaker) -> None:
    async with model_calls.track(model_calls.ASSISTANT, "x", model="m"):
        pass
    await _settled()
    async with scoped_session(wired, NON_OWNER) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.model_calls"))).scalar()
    assert count == 0
    assert (await model_calls.window(wired, NON_OWNER, seconds=900))["calls"] == []
    with pytest.raises(ProgrammingError):
        async with scoped_session(wired, NON_OWNER) as session:
            await session.execute(
                text("INSERT INTO app.model_calls (caller, model) VALUES ('assistant', 'x')")
            )
