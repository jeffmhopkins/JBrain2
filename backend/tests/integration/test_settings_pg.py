"""Migration 0012 against real Postgres: app.settings RLS isolation
(CLAUDE.md rule 3 — owner-only, the llm_usage pattern) and the settings
store's default / upsert semantics."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.settings_store import SqlSettingsStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# Even a fully domain-scoped token is not the owner: settings stay invisible.
ALL_DOMAINS = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_settings_are_owner_only(maker: async_sessionmaker[AsyncSession]) -> None:
    """Rule 3: only the owner kind reads or writes app.settings."""
    # A probe key of its own: the module-scoped database is shared with the
    # round-trip test below.
    store = SqlSettingsStore(maker)
    await store.upsert(OWNER, "rls_probe", "secret")

    async def visible(ctx: SessionContext) -> int:
        async with scoped_session(maker, ctx) as s:
            return (
                await s.execute(text("SELECT count(*) FROM app.settings WHERE key = 'rls_probe'"))
            ).scalar_one()

    assert await visible(OWNER) == 1
    assert await visible(UNSCOPED) == 0
    assert await visible(ALL_DOMAINS) == 0

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, UNSCOPED) as s:
            await s.execute(
                text("INSERT INTO app.settings (key, value) VALUES ('forged', '\"x\"'::jsonb)")
            )


async def test_store_defaults_and_upsert_round_trip(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlSettingsStore(maker)
    # An absent row reads as the caller's default — the table is never seeded.
    assert await store.get(OWNER, "image_analysis_mode", "full") == "full"
    assert await store.image_analysis_mode(OWNER) == "full"

    await store.upsert(OWNER, "image_analysis_mode", "ocr")
    assert await store.image_analysis_mode(OWNER) == "ocr"
    # Upsert means flipping back is an update, not a duplicate-key error.
    await store.upsert(OWNER, "image_analysis_mode", "full")
    assert await store.image_analysis_mode(OWNER) == "full"

    # A stored value the code no longer recognizes falls back to the default.
    await store.upsert(OWNER, "image_analysis_mode", "everything")
    assert await store.get(OWNER, "image_analysis_mode") == "everything"
    assert await store.image_analysis_mode(OWNER) == "full"


async def test_owner_timezone_round_trip_and_rejects_unknown_zones(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import OWNER_TIMEZONE_KEY

    store = SqlSettingsStore(maker)
    # Absent → None (callers fall back to UTC).
    assert await store.owner_timezone(OWNER) is None

    await store.upsert(OWNER, OWNER_TIMEZONE_KEY, "America/New_York")
    assert await store.owner_timezone(OWNER) == "America/New_York"

    # A stored value that isn't a known IANA zone reads as unset, never trusted.
    await store.upsert(OWNER, OWNER_TIMEZONE_KEY, "Mars/Olympus")
    assert await store.get(OWNER, OWNER_TIMEZONE_KEY) == "Mars/Olympus"
    assert await store.owner_timezone(OWNER) is None


async def test_llm_task_overrides_round_trip_and_sanitizes(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import LLM_TASK_OVERRIDES_KEY

    store = SqlSettingsStore(maker)
    # Absent → empty (the router then uses static config).
    assert await store.llm_task_overrides(OWNER) == {}

    await store.upsert(
        OWNER,
        LLM_TASK_OVERRIDES_KEY,
        {
            "agent.turn": {"spec": "xai:grok-4.3", "reasoning_effort": "high"},
            "note.extract": {"spec": "anthropic:claude-sonnet-4-6"},
            # Malformed entries must be dropped on read, never crash a call.
            "bad.effort": {"reasoning_effort": "extreme"},
            "junk": "not-a-dict",
        },
    )
    overrides = await store.llm_task_overrides(OWNER)
    assert overrides["agent.turn"] == {"spec": "xai:grok-4.3", "reasoning_effort": "high"}
    assert overrides["note.extract"] == {"spec": "anthropic:claude-sonnet-4-6"}
    assert "bad.effort" not in overrides and "junk" not in overrides
