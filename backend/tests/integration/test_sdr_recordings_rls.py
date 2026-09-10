"""Migration 0196 against real Postgres: `app.sdr_recordings` is owner-only.

The same policy as the APRS log and for the same reason (CLAUDE.md rule 3, enforced in
Postgres rather than in the caller): the radio is a physical device on the owner's box,
so what it recorded has no scoped-token or family case at all. A non-owner sees an EMPTY
table — not a filtered view of one — and cannot write to it.

Worth stating what this protects. A recording is raw audio off a shared channel, kept
whole: other operators' voices, and whatever the owner happened to be listening to. It
also carries `blob_sha256`, the address of the audio in the content-addressed store, and
the audio route resolves that from a row the caller could read. So a scoped token able
to SELECT here would not merely list what was recorded — it would hold the key to
playing it.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.sdr.recordings import BYTES_PER_S, RecordingsRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)

_INSERT = text(
    "INSERT INTO app.sdr_recordings"
    " (duration_s, captured_s, frequency_hz, mode, bandwidth_hz, blob_sha256, bytes, peaks)"
    " VALUES (:duration_s, :duration_s, :hz, :mode, :bw, :sha, :bytes, CAST(:peaks AS jsonb))"
)
_ROW = {
    "duration_s": 42.0,
    "hz": 162_550_000,
    "mode": "nfm",
    "bw": 16_000,
    "sha": "a" * 64,
    "bytes": 336_000,
    "peaks": "[0.1, 0.9, 0.2]",
}


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def one_recording(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_INSERT, _ROW)
        await s.commit()
    yield
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.sdr_recordings"))
        await s.commit()


async def test_the_owner_reads_their_own_recordings(
    maker: async_sessionmaker, one_recording: None
) -> None:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(text("SELECT blob_sha256, duration_s FROM app.sdr_recordings"))
        ).all()

    assert [(r.blob_sha256, r.duration_s) for r in rows] == [("a" * 64, 42.0)]


@pytest.mark.parametrize("ctx_name", ["GENERAL_ONLY", "EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_sees_nothing(
    maker: async_sessionmaker, one_recording: None, ctx_name: str
) -> None:
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]

    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(text("SELECT id, blob_sha256 FROM app.sdr_recordings"))).all()

    # EVERY_SCOPE is the interesting one: holding all four domain scopes is still not
    # being the owner, and a recording is not domain-scoped data — there is no
    # `domain_code` on this table to widen your way into.
    assert rows == []


@pytest.mark.parametrize("ctx_name", ["GENERAL_ONLY", "EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_cannot_write(maker: async_sessionmaker, ctx_name: str) -> None:
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]

    # An inserted row is a blob address the owner's library will happily serve.
    with pytest.raises((ProgrammingError, DBAPIError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(_INSERT, _ROW)
            await s.commit()


@pytest.mark.parametrize("ctx_name", ["GENERAL_ONLY", "EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_cannot_delete_a_recording(
    maker: async_sessionmaker, one_recording: None, ctx_name: str
) -> None:
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]

    async with scoped_session(maker, ctx) as s:
        await s.execute(text("DELETE FROM app.sdr_recordings"))
        await s.commit()

    # RLS makes the DELETE match no rows rather than error, so the check is that the row
    # SURVIVED. Deleting a recording is destructive twice over — the row, and then the
    # audio the api unlinks behind it.
    async with scoped_session(maker, OWNER) as s:
        rows = (await s.execute(text("SELECT id FROM app.sdr_recordings"))).all()
    assert len(rows) == 1


@pytest.mark.parametrize("ctx_name", ["GENERAL_ONLY", "EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_cannot_repoint_a_recording(
    maker: async_sessionmaker, one_recording: None, ctx_name: str
) -> None:
    """The UPDATE half, which is what a trim runs.

    A policy with USING but no WITH CHECK would let a non-owner's UPDATE match nothing
    and pass silently — but the failure that matters is the other direction: pointing an
    owner's row at a blob of someone else's choosing turns the owner-only audio route
    into a reader for any blob on the box.
    """
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]

    async with scoped_session(maker, ctx) as s:
        await s.execute(text("UPDATE app.sdr_recordings SET blob_sha256 = :sha"), {"sha": "b" * 64})
        await s.commit()

    async with scoped_session(maker, OWNER) as s:
        shas = (await s.execute(text("SELECT blob_sha256 FROM app.sdr_recordings"))).scalars().all()
    assert list(shas) == ["a" * 64]


# --- The repo, against the real schema ------------------------------------------------
#
# `RecordingsRepo` writes raw SQL against columns only this migration defines, and every
# route test above the database uses a fake repo — so a mistyped column or a jsonb cast
# that Postgres refuses would reach the owner's box with a green suite behind it. These
# run the real statements.


def _repo(maker: async_sessionmaker) -> RecordingsRepo:
    return RecordingsRepo(maker)


async def _add(maker: async_sessionmaker, **over: object) -> dict[str, object]:
    started = datetime.now(tz=UTC)
    fields: dict[str, Any] = {
        "started_at": started,
        "ended_at": started + timedelta(seconds=42),
        "duration_s": 42.0,
        "frequency_hz": 162_550_000,
        "mode": "nfm",
        "bandwidth_hz": 16_000,
        "gain": None,
        "serial": "0092",
        "blob_sha256": "a" * 64,
        "bytes_": 336_000,
        "peaks": [0.25, 0.5],
    }
    return await _repo(maker).add(OWNER, **{**fields, **over})


@pytest.fixture
async def empty_library(maker: async_sessionmaker) -> AsyncIterator[None]:
    yield
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.sdr_recordings"))
        await s.commit()


async def test_a_new_recording_starts_out_untrimmed(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """`captured_s` is set from `duration_s` at insert and never again: it is what makes
    "trimmed" derivable from the row rather than a flag that can drift."""
    saved = await _add(maker)

    assert saved["captured_s"] == saved["duration_s"] == 42.0
    assert saved["peaks"] == [0.25, 0.5]
    assert saved["has_transcript"] is False
    assert saved["blob_sha256"] == "a" * 64

    fetched = await _repo(maker).get(OWNER, str(saved["id"]))
    assert fetched is not None and fetched["bytes"] == 336_000


async def test_the_library_is_newest_first_and_hides_the_waveform(
    maker: async_sessionmaker, empty_library: None
) -> None:
    old = await _add(maker, started_at=datetime(2026, 9, 1, tzinfo=UTC), blob_sha256="b" * 64)
    new = await _add(maker, started_at=datetime(2026, 9, 9, tzinfo=UTC), blob_sha256="c" * 64)

    rows = await _repo(maker).recent(OWNER)

    assert [r["id"] for r in rows] == [new["id"], old["id"]]
    # 400 floats per row would dwarf the rest of the list response; the sheet fetches
    # the one clip it is about.
    assert "peaks" not in rows[0]


async def test_usage_prices_what_trimming_gave_back(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """`reclaimed_bytes` is derived from the seconds trimming removed, because nothing
    stores what a deleted blob weighed."""
    whole = await _add(maker, blob_sha256="d" * 64, bytes_=100)
    await _add(maker, blob_sha256="e" * 64, bytes_=200)

    before = await _repo(maker).usage(OWNER)
    assert before == {"count": 2, "bytes": 300, "reclaimed_bytes": 0}

    await _repo(maker).retrim(
        OWNER, str(whole["id"]), duration_s=12.0, blob_sha256="f" * 64, bytes_=96, peaks=[]
    )

    after = await _repo(maker).usage(OWNER)
    assert after["bytes"] == 296
    assert after["reclaimed_bytes"] == int(30.0 * BYTES_PER_S)


async def test_a_trim_repoints_the_row_and_leaves_captured_alone(
    maker: async_sessionmaker, empty_library: None
) -> None:
    saved = await _add(maker)

    updated = await _repo(maker).retrim(
        OWNER,
        str(saved["id"]),
        duration_s=9.0,
        blob_sha256="9" * 64,
        bytes_=72_000,
        peaks=[0.9],
    )

    assert updated is not None
    assert updated["duration_s"] == 9.0
    assert updated["captured_s"] == 42.0  # the whole point
    assert updated["blob_sha256"] == "9" * 64
    assert updated["peaks"] == [0.9]


async def test_removing_hands_back_the_blob_to_free(
    maker: async_sessionmaker, empty_library: None
) -> None:
    saved = await _add(maker)

    assert await _repo(maker).remove(OWNER, str(saved["id"])) == "a" * 64
    assert await _repo(maker).get(OWNER, str(saved["id"])) is None
    # Idempotent: a second delete has nothing to free, and says so rather than raising.
    assert await _repo(maker).remove(OWNER, str(saved["id"])) is None


async def test_a_blob_two_rows_share_is_reported_as_still_in_use(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """Content-addressed storage means identical clips are ONE file, and a trim to the
    full length re-puts bytes with the identical digest. This is the check that stops
    the delete-after-trim from unlinking audio a row still points at."""
    one = await _add(maker, blob_sha256="7" * 64)
    await _add(maker, blob_sha256="7" * 64)
    lonely = await _add(maker, blob_sha256="8" * 64)

    repo = _repo(maker)
    assert await repo.blob_in_use(OWNER, "7" * 64, except_id=str(one["id"])) is True
    assert await repo.blob_in_use(OWNER, "8" * 64, except_id=str(lonely["id"])) is False
    assert await repo.blob_in_use(OWNER, "8" * 64) is True
    assert await repo.blob_in_use(OWNER, "0" * 64) is False


async def test_a_non_owner_reads_an_empty_library_through_the_repo(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """The repo has no permission check of its own — Postgres is the firewall — so this
    is the assertion that it never grew one that could be wrong."""
    saved = await _add(maker)
    repo = _repo(maker)

    assert await repo.recent(EVERY_SCOPE) == []
    assert await repo.get(EVERY_SCOPE, str(saved["id"])) is None
    assert await repo.usage(EVERY_SCOPE) == {"count": 0, "bytes": 0, "reclaimed_bytes": 0}
    assert await repo.remove(EVERY_SCOPE, str(saved["id"])) is None
    assert await repo.blob_in_use(EVERY_SCOPE, "a" * 64) is False
    # ...and the row is still the owner's afterwards.
    assert await repo.get(OWNER, str(saved["id"])) is not None
