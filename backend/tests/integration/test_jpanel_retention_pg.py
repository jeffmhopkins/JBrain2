"""Voice-post retention against real Postgres: played messages age out, unplayed ones do not.

`JPANEL_PLAN.md` §5 is two sentences, and the second decides the shape of the sweep: *"30 days
after playing is a proposal. Unplayed-forever is not."* A message nobody has heard is a
four-year-old's words waiting on a wall for somebody to come back to them, and the whole
feature rests on the promise that it waits — so the case worth the most care here is the one
where the sweep does NOT run.

The other is the blob. Audio is content-addressed, so two rows with identical bytes are one
file: dropping a message's audio without asking `blob_refs` is how the owner's chat attachment
of that same clip starts 500ing, which is the fault that module was written for.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.jpanel.sweep import RETAIN_PLAYED_DAYS, sweep_played_messages
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

PANEL = uuid.uuid4()


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.jpanel_message"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        await s.execute(
            text(
                """
                INSERT INTO app.principals (id, kind, key_hash, label)
                VALUES (:id, 'device_key', :hash, 'panel Ellie')
                """
            ),
            {"id": PANEL, "hash": f"hash-{PANEL}"},
        )
        await s.commit()
    yield sessions
    await engine.dispose()


async def _message(
    sessions: async_sessionmaker[AsyncSession],
    sha: str,
    *,
    played_days_ago: int | None,
    age_days: int = 400,
) -> str:
    """One message from the panel to the owner. `played_days_ago` of None is unplayed."""
    mid = str(uuid.uuid4())
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (id, sender_kind, sender_device, recipient_kind, blob_sha256,
                     transcript, composed, duration_ms, created_at, played_at)
                VALUES (CAST(:id AS uuid), 'panel', :dev, 'owner', :sha,
                        'there is a joke', 'voice', 2000,
                        now() - make_interval(days => :age),
                        -- Cast explicitly: a parameter that only ever appears in `IS NULL`
                        -- gives asyncpg nothing to infer a type from.
                        CASE WHEN CAST(:played AS int) IS NULL THEN NULL
                             ELSE now() - make_interval(days => CAST(:played AS int)) END)
                """
            ),
            # `sender_device` is TEXT — it holds a principal id as the routes write it, which
            # is the string form.
            {"id": mid, "dev": str(PANEL), "sha": sha, "age": age_days, "played": played_days_ago},
        )
        await s.commit()
    return mid


async def _live(sessions: async_sessionmaker[AsyncSession]) -> set[str]:
    async with scoped_session(sessions, OWNER) as s:
        rows = (await s.execute(text("SELECT id::text FROM app.jpanel_message"))).all()
    return {str(r[0]) for r in rows}


async def test_an_unheard_message_is_never_old_enough_to_sweep(
    maker: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """THE PROMISE THE FEATURE RESTS ON. A message with no `played_at` is a message nobody has
    come back to yet, and its `created_at` says nothing about that — so a year-old unplayed
    row is not a stale row, it is a four-year-old's words still waiting."""
    blobs = FsBlobStore(tmp_path)
    sha = await blobs.put(b"unheard words")
    kept = await _message(maker, sha, played_days_ago=None, age_days=400)

    deleted, freed = await sweep_played_messages(maker, blobs, OWNER)

    assert (deleted, freed) == (0, 0)
    assert await _live(maker) == {kept}
    assert await blobs.exists(sha), "and its audio is still there to play"


async def test_a_played_message_goes_only_once_it_is_past_the_window(
    maker: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    blobs = FsBlobStore(tmp_path)
    recent_sha = await blobs.put(b"played yesterday")
    old_sha = await blobs.put(b"played long ago")
    recent = await _message(maker, recent_sha, played_days_ago=1)
    await _message(maker, old_sha, played_days_ago=RETAIN_PLAYED_DAYS + 1)

    deleted, freed = await sweep_played_messages(maker, blobs, OWNER)

    assert (deleted, freed) == (1, 1)
    assert await _live(maker) == {recent}
    assert await blobs.exists(recent_sha)
    assert not await blobs.exists(old_sha)


async def test_the_window_is_measured_from_when_it_was_heard_not_when_it_was_sent(
    maker: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """A message sent a year ago and played this morning is a message from this morning as far
    as retention is concerned — the owner just listened to it."""
    blobs = FsBlobStore(tmp_path)
    sha = await blobs.put(b"old words, heard today")
    kept = await _message(maker, sha, played_days_ago=0, age_days=365)

    assert await sweep_played_messages(maker, blobs, OWNER) == (0, 0)
    assert await _live(maker) == {kept}


async def test_audio_another_row_still_holds_is_not_unlinked(
    maker: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """THE FAULT `blob_refs` WAS WRITTEN FOR, and the reason this table had to join its list.

    `app.jpanel_message.blob_sha256` was added by migration 0208 and never registered in
    `BLOB_REFERENCES`, so nothing on the box counted a voice message as holding its own file.
    Here the holder is an UNPLAYED message sharing a digest with a swept one — Dad's good-night
    to both twins is one file, and one of them has not listened yet. Without the registry entry
    the sweep would take her sister's copy with it: the row she still has would point at a file
    that is gone, and the only symptom would be a 500 when a four-year-old pressed play.

    The cross-feature version of the same hazard — the owner downloading a message from the PWA
    and attaching it to a chat — runs through the identical mechanism, and
    `test_sdr_recordings_rls.py` covers that shape with a real chat attachment."""
    blobs = FsBlobStore(tmp_path)
    sha = await blobs.put(b"good night, both of you")
    await _message(maker, sha, played_days_ago=RETAIN_PLAYED_DAYS + 5)
    unheard = await _message(maker, sha, played_days_ago=None)

    deleted, freed = await sweep_played_messages(maker, blobs, OWNER)

    assert deleted == 1, "the played copy still ages out"
    assert freed == 0, "but the file is the unheard copy's too"
    assert await _live(maker) == {unheard}
    assert await blobs.exists(sha), "so she can still hear it"


async def test_two_messages_sharing_one_file_free_it_once(
    maker: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """Dad sending the same words twice produces identical TTS bytes and therefore one file.
    Both rows go; the file is collected once, and the count says once rather than twice."""
    blobs = FsBlobStore(tmp_path)
    sha = await blobs.put(b"good night, sleep tight")
    await _message(maker, sha, played_days_ago=RETAIN_PLAYED_DAYS + 2)
    await _message(maker, sha, played_days_ago=RETAIN_PLAYED_DAYS + 3)

    assert await sweep_played_messages(maker, blobs, OWNER) == (2, 1)
    assert await _live(maker) == set()
    assert not await blobs.exists(sha)


async def test_a_sweep_with_nothing_to_do_says_so_without_touching_the_store(
    maker: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    blobs = FsBlobStore(tmp_path)
    assert await sweep_played_messages(maker, blobs, OWNER) == (0, 0)
