"""The MQTT ingest consumer's topic parsing + message handler (unit, no broker)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.auth import keys
from jbrain.locations import LocationFix
from jbrain.mqtt.consumer import handle_message, principal_id_from_topic
from tests.unit.fakes import FakeAuthRepo

# detect_transitions opens a session on this unreachable DB and fails closed
# internally (best-effort), so the handler still returns the ingest result.
_DUMMY_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_LOC = b'{"_type":"location","lat":40.0,"lon":-74.0,"tst":1700000000}'


@dataclass
class FakeSink:
    calls: list[tuple[str, str, LocationFix]] = field(default_factory=list)
    dup: bool = False

    async def ingest_fix(self, *, principal_id: str, subject_id: str, fix: LocationFix) -> bool:
        self.calls.append((principal_id, subject_id, fix))
        return not self.dup


@pytest.fixture
async def maker() -> AsyncIterator[async_sessionmaker]:
    engine = create_async_engine(_DUMMY_DB)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _device() -> tuple[FakeAuthRepo, str]:
    repo = FakeAuthRepo()
    await repo.create_principal("device_key", keys.hash_key("k"), "phone", subject_id="subj-9")
    return repo, repo.principals[0].id


# --- principal_id_from_topic -------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("owntracks/abc/phone", "abc"),
        ("owntracks/abc/phone/cmd", None),  # a subtopic, not a base location topic
        ("owntracks/abc", None),  # too few segments
        ("owntracks//phone", None),  # empty owner segment
        ("system/abc/phone", None),  # foreign root
        ("abc", None),
    ],
)
def test_principal_id_from_topic(topic: str, expected: str | None) -> None:
    assert principal_id_from_topic(topic) == expected


# --- handle_message ----------------------------------------------------------


async def test_valid_location_ingested_under_the_topic_owners_subject(
    maker: async_sessionmaker,
) -> None:
    repo, pid = await _device()
    sink = FakeSink()
    stored = await handle_message(repo, sink, maker, topic=f"owntracks/{pid}/phone", payload=_LOC)
    assert stored is True
    assert len(sink.calls) == 1
    p, s, fix = sink.calls[0]
    assert (p, s) == (pid, "subj-9")  # subject from the principal, never the payload
    assert (fix.latitude, fix.longitude) == (40.0, -74.0)


async def test_non_location_body_is_ignored(maker: async_sessionmaker) -> None:
    repo, pid = await _device()
    sink = FakeSink()
    out = await handle_message(
        repo, sink, maker, topic=f"owntracks/{pid}/phone", payload=b'{"_type":"transition"}'
    )
    assert out is False
    assert sink.calls == []


async def test_unknown_principal_is_dropped(maker: async_sessionmaker) -> None:
    repo, _ = await _device()
    sink = FakeSink()
    unknown = "00000000-0000-0000-0000-000000000000"
    out = await handle_message(repo, sink, maker, topic=f"owntracks/{unknown}/phone", payload=_LOC)
    assert out is False
    assert sink.calls == []


async def test_subtopic_bad_json_and_schema_invalid_are_dropped(maker: async_sessionmaker) -> None:
    repo, pid = await _device()
    sink = FakeSink()
    base = f"owntracks/{pid}/phone"
    assert await handle_message(repo, sink, maker, topic=f"{base}/cmd", payload=_LOC) is False
    assert await handle_message(repo, sink, maker, topic=base, payload=b"not json") is False
    bad = b'{"_type":"location","lat":999,"lon":-74.0,"tst":1700000000}'
    assert await handle_message(repo, sink, maker, topic=base, payload=bad) is False
    assert sink.calls == []
