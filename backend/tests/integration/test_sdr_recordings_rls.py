"""Migration 0198 against real Postgres: `app.sdr_recordings` is owner-only.

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
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.api import sdr as sdr_api
from jbrain.blob_refs import BLOB_REFERENCES, blob_referenced
from jbrain.db.session import SessionContext, scoped_session
from jbrain.sdr.audio import Cut
from jbrain.sdr.recordings import BYTES_PER_S, RecordingsRepo
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

#: The digest a recording and a chat attachment both hold — one file, two owners.
SHARED_SHA = "5" * 64

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
async def test_a_non_owner_cannot_read_a_transcript_either(
    maker: async_sessionmaker, ctx_name: str
) -> None:
    """A captions row is a different kind of exposure from a clip. An audio row hands out
    an address that has to be resolved before anything can be heard; a transcript is the
    words themselves, already in the column — other operators' names, callsigns and
    traffic, readable straight off a SELECT. The same policy covers it, and this is the
    test that says so rather than assuming it because the rows share a table."""
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.sdr_recordings (kind, frequency_hz, mode, transcript)"
                " VALUES ('captions', 146520000, 'nfm', CAST(:t AS jsonb))"
            ),
            {"t": '{"text": "net control, K7XYZ"}'},
        )
        await s.commit()
    try:
        async with scoped_session(maker, ctx) as s:
            rows = (await s.execute(text("SELECT transcript FROM app.sdr_recordings"))).all()
        assert rows == []
    finally:
        async with scoped_session(maker, OWNER) as s:
            await s.execute(text("DELETE FROM app.sdr_recordings"))
            await s.commit()


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

    removed = await _repo(maker).remove(OWNER, str(saved["id"]))
    assert removed is not None and removed["blob_sha256"] == "a" * 64
    assert await _repo(maker).get(OWNER, str(saved["id"])) is None
    # Idempotent: a second delete has nothing to free, and says so rather than raising.
    # None here means "no such row" specifically — a captions row deletes to a row whose
    # `blob_sha256` is None, which is a different answer and must not read as this one.
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


async def _add_captions(maker: async_sessionmaker, **over: object) -> dict[str, object]:
    started = datetime.now(tz=UTC)
    fields: dict[str, Any] = {
        "started_at": started,
        "ended_at": started + timedelta(seconds=600),
        "duration_s": 600.0,
        "frequency_hz": 146_520_000,
        "mode": "nfm",
        "bandwidth_hz": None,
        "gain": None,
        "serial": "0092",
        "transcript": {
            "text": "net control, K7XYZ",
            "words": [{"text": "net", "start_ms": 0, "end_ms": 200, "confidence": 0.91}],
            "duration_ms": 600_000,
            "segments": [{"at_s": 0.0, "text": "net control, K7XYZ"}],
        },
        "transcribed_at": started + timedelta(seconds=600),
    }
    return await _repo(maker).add_captions(OWNER, **{**fields, **over})


async def test_a_captions_row_round_trips_with_no_audio_columns_at_all(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """Migration 0201, against the real table. NULL is the assertion: `bytes = 0` and
    `peaks = []` would be two measurements of a file that does not exist, and the column
    defaults that produced them are gone precisely so an INSERT cannot invent them."""
    saved = await _add_captions(maker)

    assert saved["kind"] == "captions"
    assert saved["blob_sha256"] is None
    assert saved["bytes"] is None
    assert saved["peaks"] is None
    assert saved["has_transcript"] is True
    assert cast(dict[str, Any], saved["transcript"])["text"] == "net control, K7XYZ"
    # Untrimmed for ever: nothing cuts a transcript, so the two stay equal.
    assert saved["captured_s"] == saved["duration_s"] == 600.0

    fetched = await _repo(maker).get(OWNER, str(saved["id"]))
    assert fetched is not None
    assert fetched["transcript"]["segments"] == [{"at_s": 0.0, "text": "net control, K7XYZ"}]
    assert fetched["transcribed_at"] is not None


async def test_an_existing_recording_is_still_audio_without_being_told_so(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """The default carries every row the box already has. `add` does not name `kind` at
    all — it is the statement that shipped before there were kinds — so this is also the
    check that the audio path was not quietly asked to start saying what it is."""
    saved = await _add(maker)

    assert saved["kind"] == "audio"


async def test_the_table_refuses_a_row_that_is_neither_shape(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """The CHECK is what makes "a captions row has no blob" a schema fact rather than a
    convention every future writer has to remember — and the writer who forgets would be
    found by `blob_refs` handing out a NULL digest, or by a disk meter counting a
    transcript as audio."""
    async with scoped_session(maker, OWNER) as session:
        with pytest.raises((DBAPIError, ProgrammingError)):
            await session.execute(
                text(
                    "INSERT INTO app.sdr_recordings (kind, frequency_hz, mode, blob_sha256,"
                    " bytes, peaks) VALUES ('captions', 1, 'nfm', :sha, 0, '[]'::jsonb)"
                ),
                {"sha": "a" * 64},
            )
        await session.rollback()

    async with scoped_session(maker, OWNER) as session:
        with pytest.raises((DBAPIError, ProgrammingError)):
            await session.execute(
                text(
                    "INSERT INTO app.sdr_recordings (kind, frequency_hz, mode)"
                    " VALUES ('audio', 1, 'nfm')"
                )
            )
        await session.rollback()


async def test_usage_never_prices_a_transcript_at_the_audio_bitrate(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """The disk meter is the only argument for deleting anything — nothing expires on its
    own — so a captions row counted at 8 kB/s would report space that was never taken,
    and ten minutes of it would read as 4.8 MB the owner could free by deleting words."""
    await _add(maker, blob_sha256="d" * 64, bytes_=100)
    before = await _repo(maker).usage(OWNER)

    await _add_captions(maker)
    after = await _repo(maker).usage(OWNER)

    assert before["bytes"] == after["bytes"] == 100
    assert after["reclaimed_bytes"] == 0
    # The COUNT is the library's, which is what the header sits above.
    assert (before["count"], after["count"]) == (1, 2)


async def test_a_captions_row_deletes_without_a_blob_to_free(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """`remove` hands back the ROW: None means no such recording, and a row carrying a
    NULL digest means there was never a file. Read as a bare sha the two are one answer,
    and the owner gets a 404 for a delete that happened."""
    saved = await _add_captions(maker)

    removed = await _repo(maker).remove(OWNER, str(saved["id"]))

    assert removed is not None
    assert removed["kind"] == "captions"
    assert removed["blob_sha256"] is None
    assert await _repo(maker).get(OWNER, str(saved["id"])) is None


async def test_a_captions_row_holds_no_blob_open(
    maker: async_sessionmaker, empty_library: None
) -> None:
    """`blob_sha256 = :sha` is NULL rather than true for a row with no digest, so a
    transcript neither keeps a clip alive nor is mistaken for keeping one."""
    await _add_captions(maker)

    assert await _repo(maker).blob_in_use(OWNER, "a" * 64) is False


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


# --- The blob guard, against every table that can hold one ----------------------------
#
# `SDR_RECORDING_PLAN.md` §7 argued that no other table could share a recording's blob
# "by accident", and checked only `app.sdr_recordings` before unlinking. That is false
# today: the PWA offers Download (.mp3), `agent/attachments.py` allow-lists `audio/mpeg`
# so the owner can attach audio to a chat, and `api/chat_attachments.py` stores it with
# `blobs.put(data)`. Identical bytes, identical digest, ONE file, two owners — and
# deleting the recording unlinked the attachment's audio.
#
# These run the real list against the real schema, because the list is a hand-kept
# mapping of table and column names: nothing above the database can tell that one of them
# has been renamed, and the symptom of getting it wrong is somebody else's file
# disappearing months later.

#: The owner principal an agent session hangs off. A fresh template has none — a real box
#: gets one from the first key rotation — so this makes the minimum one the FK needs.
_OWNER_PRINCIPAL = text(
    "WITH existing AS (SELECT id FROM app.principals WHERE kind = 'owner' LIMIT 1),"
    " made AS ("
    "  INSERT INTO app.principals (id, kind, key_hash, label)"
    "  SELECT gen_random_uuid(), 'owner', 'test-' || gen_random_uuid()::text, 'test owner'"
    "  WHERE NOT EXISTS (SELECT 1 FROM existing) RETURNING id)"
    " SELECT id FROM existing UNION ALL SELECT id FROM made"
)


async def test_every_reference_in_the_list_is_a_real_table_and_column(
    maker: async_sessionmaker,
) -> None:
    """`blob_referenced` fails CLOSED, so a clause that does not compile answers True and
    the blob is merely kept — safe, but it would silently stop anything ever being freed.
    On an empty schema the honest answer is False, which is only reachable if every
    clause ran."""
    async with scoped_session(maker, OWNER) as s:
        assert await blob_referenced(s, "0" * 64) is False

    # ...and again one at a time, so a rename says WHICH table it broke rather than
    # failing the composed statement with one message about all of them.
    for ref in BLOB_REFERENCES:
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(f"SELECT 1 FROM {ref.table} WHERE ({ref.where}) LIMIT 1"), {"sha": "0" * 64}
            )


@pytest.fixture
async def a_chat_attachment(maker: async_sessionmaker) -> AsyncIterator[str]:
    """The owner downloaded a recording and attached the .mp3 to a chat.

    Not a contrivance: Download (.mp3) is on the recording card, `audio/mpeg` is
    allow-listed for chat attachments precisely so audio can be attached, and the store
    is content-addressed — so the attachment and the recording are the same file the
    moment the bytes are the same.
    """
    async with scoped_session(maker, OWNER) as s:
        principal = (await s.execute(_OWNER_PRINCIPAL)).scalar()
        session_id = (
            await s.execute(
                text(
                    "INSERT INTO app.agent_sessions (id, principal_id, domain_scopes)"
                    " VALUES (gen_random_uuid(), :pid, ARRAY['general']) RETURNING id"
                ),
                {"pid": principal},
            )
        ).scalar()
        await s.execute(
            text(
                "INSERT INTO app.turn_attachments"
                " (id, session_id, domain_code, sha256, filename, media_type, size_bytes)"
                " VALUES (gen_random_uuid(), :sid, 'general', :sha, 'net-control.mp3',"
                " 'audio/mpeg', 336000)"
            ),
            {"sid": session_id, "sha": SHARED_SHA},
        )
        await s.commit()
    yield SHARED_SHA
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.turn_attachments"))
        await s.execute(text("DELETE FROM app.agent_sessions"))
        await s.execute(text("DELETE FROM app.principals WHERE label = 'test owner'"))
        await s.commit()


async def test_a_recordings_blob_is_in_use_when_a_chat_attachment_shares_it(
    maker: async_sessionmaker, empty_library: None, a_chat_attachment: str
) -> None:
    """The reproduced path, at the level that decides whether to unlink.

    Before this, `blob_in_use` asked `app.sdr_recordings` and nothing else — so with the
    recording's row already deleted the answer was False, the file went, and the chat
    attachment's download started returning 500 with nothing to say why."""
    saved = await _add(maker, blob_sha256=a_chat_attachment)
    repo = _repo(maker)

    assert await repo.blob_in_use(OWNER, a_chat_attachment) is True
    # Even excepting the recording itself — which is what a trim asks, and what a DELETE
    # effectively asks once its row is gone.
    assert await repo.blob_in_use(OWNER, a_chat_attachment, except_id=str(saved["id"])) is True


async def test_a_digest_nothing_else_holds_is_still_free_to_delete(
    maker: async_sessionmaker, empty_library: None, a_chat_attachment: str
) -> None:
    """The guard has to be able to say no, or a trim frees nothing and the feature
    inverts (the plan's §5)."""
    lonely = await _add(maker, blob_sha256="3" * 64)

    assert await _repo(maker).blob_in_use(OWNER, "3" * 64, except_id=str(lonely["id"])) is False


# --- ...and end to end, through the routes that actually unlink ------------------------


class _Owner:
    """What `OwnerDep` hands a route: `ctx_for` reads only these two fields."""

    def __init__(self, principal_id: str) -> None:
        self.id = principal_id
        self.kind = "owner"


@pytest.fixture
async def owner_principal(maker: async_sessionmaker) -> str:
    async with scoped_session(maker, OWNER) as s:
        principal = (await s.execute(_OWNER_PRINCIPAL)).scalar()
        await s.commit()
    return str(principal)


async def test_deleting_a_recording_keeps_audio_a_chat_attachment_still_holds(
    maker: async_sessionmaker,
    empty_library: None,
    a_chat_attachment: str,
    owner_principal: str,
    tmp_path: Path,
) -> None:
    """**The reproduced data loss, through the route that caused it.**

    Download a recording, attach it to a chat, delete the recording. Content-addressing
    means the two are one file, so the delete unlinked the attachment's bytes too and its
    download started answering 500. The recording's ROW must still go — that is what was
    asked for — and the file must stay."""
    blobs = FsBlobStore(tmp_path)
    audio = b"the net control recording, also sitting in a chat"
    sha = await blobs.put(audio)
    saved = await _add(maker, blob_sha256=sha)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("UPDATE app.turn_attachments SET sha256 = :sha"), {"sha": sha})
        await s.commit()

    out = await sdr_api.delete_recording(
        str(saved["id"]),
        _Owner(owner_principal),  # type: ignore[arg-type]
        _repo(maker),
        blobs,
    )

    assert out["deleted"] is True
    assert await _repo(maker).get(OWNER, str(saved["id"])) is None  # the row is gone...
    assert await blobs.exists(sha)  # ...and the attachment can still be downloaded
    assert blobs.path_for(sha).read_bytes() == audio


async def test_trimming_a_recording_keeps_audio_a_chat_attachment_still_holds(
    maker: async_sessionmaker,
    empty_library: None,
    a_chat_attachment: str,
    owner_principal: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same file, through the other route that unlinks. A trim repoints the row at
    the cut clip and frees the original — but "the original" is also somebody's
    attachment, and freeing it there is the same loss by a different door."""
    blobs = FsBlobStore(tmp_path)
    audio = b"the whole capture, also sitting in a chat"
    sha = await blobs.put(audio)
    saved = await _add(maker, blob_sha256=sha, duration_s=42.0)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("UPDATE app.turn_attachments SET sha256 = :sha"), {"sha": sha})
        await s.commit()

    async def fake_cut(source: Path, start_s: float, end_s: float) -> Cut:
        return Cut(data=b"the kept part", peaks=[0.4], duration_s=9.0)

    monkeypatch.setattr(sdr_api, "cut_clip", fake_cut)

    out = await sdr_api.trim_recording(
        str(saved["id"]),
        sdr_api.TrimIn(start_s=4.0, end_s=13.0),
        _Owner(owner_principal),  # type: ignore[arg-type]
        _repo(maker),
        blobs,
    )

    assert out["recording"]["duration_s"] == 9.0  # the row moved to the trimmed audio...
    assert await blobs.exists(sha)  # ...and the attachment's file is still there
    assert blobs.path_for(sha).read_bytes() == audio
