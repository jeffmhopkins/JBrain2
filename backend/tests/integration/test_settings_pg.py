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


async def test_free_ram_fraction_round_trips_and_sanitizes(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import LLM_LOCAL_FREE_RAM_FRACTION_KEY

    store = SqlSettingsStore(maker)
    # Absent → None (the caller falls back to the config default floor).
    assert await store.llm_local_free_ram_fraction(OWNER) is None

    # A valid fraction round-trips.
    assert await store.set_llm_local_free_ram_fraction(OWNER, 0.2) == 0.2
    assert await store.llm_local_free_ram_fraction(OWNER) == 0.2

    # Clearing (None) reverts to the config default (stored as null, read as None).
    assert await store.set_llm_local_free_ram_fraction(OWNER, None) is None
    assert await store.llm_local_free_ram_fraction(OWNER) is None

    # Junk in the row never reads as a floor — out of (0, 1), a bool, or a non-number all
    # fall back to None rather than budgeting the box off a bad value.
    for junk in (1.5, 0.0, 1.0, True, "lots"):
        await store.upsert(OWNER, LLM_LOCAL_FREE_RAM_FRACTION_KEY, junk)
        assert await store.llm_local_free_ram_fraction(OWNER) is None, junk


async def test_brain_llm_stream_defaults_off_and_round_trips(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import BRAIN_LLM_STREAM_KEY

    store = SqlSettingsStore(maker)
    # Absent → off: owner text never rides the unauthenticated display by default.
    assert await store.brain_llm_stream(OWNER) is False

    await store.upsert(OWNER, BRAIN_LLM_STREAM_KEY, True)
    assert await store.brain_llm_stream(OWNER) is True

    # Any non-true stored value reads as off (fail-closed — junk never enables it).
    await store.upsert(OWNER, BRAIN_LLM_STREAM_KEY, "on")
    assert await store.brain_llm_stream(OWNER) is False
    await store.upsert(OWNER, BRAIN_LLM_STREAM_KEY, False)
    assert await store.brain_llm_stream(OWNER) is False


async def test_brain_read_aloud_defaults_off_and_round_trips(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import BRAIN_READ_ALOUD_KEY

    store = SqlSettingsStore(maker)
    # Absent → off: the wall shows no voice panel until the owner opts in.
    assert await store.brain_read_aloud(OWNER) is False

    await store.upsert(OWNER, BRAIN_READ_ALOUD_KEY, True)
    assert await store.brain_read_aloud(OWNER) is True

    # Any non-true stored value reads as off (fail-closed).
    await store.upsert(OWNER, BRAIN_READ_ALOUD_KEY, "on")
    assert await store.brain_read_aloud(OWNER) is False
    await store.upsert(OWNER, BRAIN_READ_ALOUD_KEY, False)
    assert await store.brain_read_aloud(OWNER) is False


async def test_tavily_settings_default_on_keyless_and_round_trip(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import TAVILY_ENABLED_KEY

    store = SqlSettingsStore(maker)
    # Absent → enabled ON (single-owner box ships it on) but keyless (inert until a key is set).
    assert await store.tavily_enabled(OWNER) is True
    assert await store.tavily_api_key(OWNER) == ""

    # The toggle round-trips; any non-true stored value reads as off (fail-closed).
    await store.set_tavily_enabled(OWNER, False)
    assert await store.tavily_enabled(OWNER) is False
    await store.set_tavily_enabled(OWNER, True)
    assert await store.tavily_enabled(OWNER) is True
    await store.upsert(OWNER, TAVILY_ENABLED_KEY, "on")
    assert await store.tavily_enabled(OWNER) is False

    # The key round-trips; clearing reverts to "" (the caller then uses the env fallback); a
    # non-string store reads as unset rather than as a key.
    await store.set_tavily_api_key(OWNER, "tvly-secret")
    assert await store.tavily_api_key(OWNER) == "tvly-secret"
    await store.set_tavily_api_key(OWNER, "")
    assert await store.tavily_api_key(OWNER) == ""
    from jbrain.settings_store import TAVILY_API_KEY_KEY

    await store.upsert(OWNER, TAVILY_API_KEY_KEY, 123)
    assert await store.tavily_api_key(OWNER) == ""


async def test_tavily_settings_are_owner_only(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # The Tavily key/toggle ride the owner-RLS app.settings table: a write by the owner is
    # invisible to a non-owner session, and the reads fall back to their defaults there.
    store = SqlSettingsStore(maker)
    await store.set_tavily_api_key(OWNER, "tvly-secret")
    await store.set_tavily_enabled(OWNER, False)
    assert await store.tavily_api_key(UNSCOPED) == ""
    assert await store.tavily_enabled(UNSCOPED) is True  # non-owner sees the default, not the row


async def test_brain_answer_voice_defaults_to_amy_and_round_trips(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import BRAIN_ANSWER_VOICE_KEY

    store = SqlSettingsStore(maker)
    # Absent → Amy, so read-aloud always has a valid voice.
    assert await store.brain_answer_voice(OWNER) == "kokoro-af_heart"

    await store.upsert(OWNER, BRAIN_ANSWER_VOICE_KEY, "kokoro-am_michael")
    assert await store.brain_answer_voice(OWNER) == "kokoro-am_michael"

    # A non-string / empty stored value reads back as the default.
    await store.upsert(OWNER, BRAIN_ANSWER_VOICE_KEY, "")
    assert await store.brain_answer_voice(OWNER) == "kokoro-af_heart"
    await store.upsert(OWNER, BRAIN_ANSWER_VOICE_KEY, 5)
    assert await store.brain_answer_voice(OWNER) == "kokoro-af_heart"


async def test_brain_read_aloud_engine_defaults_piper_and_round_trips(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import BRAIN_READ_ALOUD_ENGINE_KEY

    store = SqlSettingsStore(maker)
    # Absent → piper (on-box, native fallback).
    assert await store.brain_read_aloud_engine(OWNER) == "piper"

    await store.upsert(OWNER, BRAIN_READ_ALOUD_ENGINE_KEY, "native")
    assert await store.brain_read_aloud_engine(OWNER) == "native"

    # An unrecognized value reads back as the default.
    await store.upsert(OWNER, BRAIN_READ_ALOUD_ENGINE_KEY, "robot")
    assert await store.brain_read_aloud_engine(OWNER) == "piper"


async def test_brain_voice_effects_default_to_no_ops_and_clamp_on_read(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import (
        BRAIN_ANSWER_CHORUS_KEY,
        BRAIN_ANSWER_PITCH_KEY,
        BRAIN_ANSWER_ROBOT_KEY,
        BRAIN_ANSWER_SPEED_KEY,
    )

    store = SqlSettingsStore(maker)
    # Absent → no-ops (1.0× / 0 st / off), so an untouched box is unchanged.
    assert await store.brain_answer_speed(OWNER) == 1.0
    assert await store.brain_answer_pitch(OWNER) == 0.0
    assert await store.brain_answer_chorus(OWNER) is False
    assert await store.brain_answer_robot(OWNER) is False

    await store.upsert(OWNER, BRAIN_ANSWER_SPEED_KEY, 1.25)
    await store.upsert(OWNER, BRAIN_ANSWER_PITCH_KEY, -3.0)
    await store.upsert(OWNER, BRAIN_ANSWER_CHORUS_KEY, True)
    await store.upsert(OWNER, BRAIN_ANSWER_ROBOT_KEY, True)
    assert await store.brain_answer_speed(OWNER) == 1.25
    assert await store.brain_answer_pitch(OWNER) == -3.0
    assert await store.brain_answer_chorus(OWNER) is True
    assert await store.brain_answer_robot(OWNER) is True

    # An out-of-range or non-numeric stored value is clamped / defaulted on read.
    await store.upsert(OWNER, BRAIN_ANSWER_SPEED_KEY, 9.0)
    assert await store.brain_answer_speed(OWNER) == 2.0
    await store.upsert(OWNER, BRAIN_ANSWER_PITCH_KEY, -99.0)
    assert await store.brain_answer_pitch(OWNER) == -12.0
    await store.upsert(OWNER, BRAIN_ANSWER_SPEED_KEY, "fast")
    assert await store.brain_answer_speed(OWNER) == 1.0


async def test_pronunciation_lexicon_round_trips_and_sanitizes(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlSettingsStore(maker)
    # Absent → empty map (today's behavior).
    assert await store.pronunciation_lexicon(OWNER) == {}

    stored = await store.set_pronunciation_lexicon(
        OWNER, {"Titusville": "Tight us ville", "  ": "x", "y": "  ", "ok": "okay"}
    )
    # Blank word / blank respelling are dropped; the good entries persist and read back.
    assert stored == {"Titusville": "Tight us ville", "ok": "okay"}
    assert await store.pronunciation_lexicon(OWNER) == {
        "Titusville": "Tight us ville",
        "ok": "okay",
    }

    # A junk stored value reads back as empty rather than crashing a render.
    from jbrain.settings_store import PRONUNCIATION_LEXICON_KEY

    await store.upsert(OWNER, PRONUNCIATION_LEXICON_KEY, "not a dict")
    assert await store.pronunciation_lexicon(OWNER) == {}


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


async def test_llm_local_context_windows_round_trip_and_sanitizes(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import LLM_LOCAL_CONTEXT_WINDOWS_KEY

    store = SqlSettingsStore(maker)
    assert await store.llm_local_context_windows(OWNER) == {}

    # set/clear round-trips through the single row.
    await store.set_llm_local_context_window(OWNER, model_id="gpt-oss-120b", window=65536)
    assert await store.llm_local_context_windows(OWNER) == {"gpt-oss-120b": 65536}
    await store.set_llm_local_context_window(OWNER, model_id="gpt-oss-120b", window=None)
    assert await store.llm_local_context_windows(OWNER) == {}

    # A junk value (non-positive, bool, non-int, non-dict store) never reads as a window.
    await store.upsert(
        OWNER,
        LLM_LOCAL_CONTEXT_WINDOWS_KEY,
        {"gpt-oss-120b": 0, "qwen3-vl-30b": True, "x": "lots", "ok": 16384},
    )
    assert await store.llm_local_context_windows(OWNER) == {"ok": 16384}


async def test_llm_local_parallel_slots_round_trip_and_sanitizes(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import LLM_LOCAL_PARALLEL_SLOTS_KEY

    store = SqlSettingsStore(maker)
    assert await store.llm_local_parallel_slots(OWNER) == {}

    # set 2 / clear round-trips; 1 (the single-slot default) is stored as an absence.
    await store.set_llm_local_parallel_slots(OWNER, model_id="gpt-oss-120b", slots=2)
    assert await store.llm_local_parallel_slots(OWNER) == {"gpt-oss-120b": 2}
    await store.set_llm_local_parallel_slots(OWNER, model_id="gpt-oss-120b", slots=1)
    assert await store.llm_local_parallel_slots(OWNER) == {}

    # Only ints > 1 survive: 1, bools, non-ints, and a non-dict store all read as no override.
    await store.upsert(
        OWNER,
        LLM_LOCAL_PARALLEL_SLOTS_KEY,
        {"a": 1, "b": True, "c": "two", "gpt-oss-120b": 2},
    )
    assert await store.llm_local_parallel_slots(OWNER) == {"gpt-oss-120b": 2}


async def test_llm_local_unavailable_round_trip_and_dedups(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import LLM_LOCAL_UNAVAILABLE_KEY

    store = SqlSettingsStore(maker)
    assert await store.llm_local_unavailable(OWNER) == []

    await store.set_llm_local_unavailable(OWNER, ["gpt-oss-120b", "qwen3-vl-30b", "gpt-oss-120b"])
    assert await store.llm_local_unavailable(OWNER) == ["gpt-oss-120b", "qwen3-vl-30b"]

    # Non-list / non-string entries are dropped on read.
    await store.upsert(OWNER, LLM_LOCAL_UNAVAILABLE_KEY, ["a", 5, "a", None, "b"])
    assert await store.llm_local_unavailable(OWNER) == ["a", "b"]


async def test_llm_local_provision_requested_round_trip_and_dedups(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import LLM_LOCAL_PROVISION_REQUESTED_KEY

    store = SqlSettingsStore(maker)
    assert await store.llm_local_provision_requested(OWNER) == []

    await store.set_llm_local_provision_requested(
        OWNER, ["nemotron-3-super-120b", "nemotron-3-super-120b"]
    )
    assert await store.llm_local_provision_requested(OWNER) == ["nemotron-3-super-120b"]

    # Non-list / non-string entries are dropped on read.
    await store.upsert(OWNER, LLM_LOCAL_PROVISION_REQUESTED_KEY, ["a", 5, "a", None, "b"])
    assert await store.llm_local_provision_requested(OWNER) == ["a", "b"]

    # Clearing empties the queue (what the update one-shot does post-provision).
    await store.set_llm_local_provision_requested(OWNER, [])
    assert await store.llm_local_provision_requested(OWNER) == []


async def test_llm_local_remove_requested_round_trip_and_dedups(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    from jbrain.settings_store import LLM_LOCAL_REMOVE_REQUESTED_KEY

    store = SqlSettingsStore(maker)
    assert await store.llm_local_remove_requested(OWNER) == []

    await store.set_llm_local_remove_requested(OWNER, ["gpt-oss-120b", "gpt-oss-120b"])
    assert await store.llm_local_remove_requested(OWNER) == ["gpt-oss-120b"]

    # Non-list / non-string entries are dropped on read.
    await store.upsert(OWNER, LLM_LOCAL_REMOVE_REQUESTED_KEY, ["a", 5, "a", None, "b"])
    assert await store.llm_local_remove_requested(OWNER) == ["a", "b"]

    # Clearing empties the queue (what the update one-shot does post-uninstall).
    await store.set_llm_local_remove_requested(OWNER, [])
    assert await store.llm_local_remove_requested(OWNER) == []


async def test_llm_local_settings_are_owner_only(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # The new keys ride the owner-RLS app.settings table: a window/queue write by the
    # owner is invisible to a non-owner session.
    store = SqlSettingsStore(maker)
    await store.set_llm_local_context_window(OWNER, model_id="gpt-oss-120b", window=65536)
    await store.set_llm_local_unavailable(OWNER, ["gpt-oss-120b"])
    await store.set_llm_local_provision_requested(OWNER, ["nemotron-3-super-120b"])
    await store.set_llm_local_remove_requested(OWNER, ["gpt-oss-120b"])
    assert await store.llm_local_context_windows(UNSCOPED) == {}
    assert await store.llm_local_unavailable(UNSCOPED) == []
    assert await store.llm_local_provision_requested(UNSCOPED) == []
    assert await store.llm_local_remove_requested(UNSCOPED) == []
