"""Wave 1, slice 2 — three-valued `current()` against real Postgres.

The three read surfaces that previously had no assertion filter (entity_view,
note_currency, canonical name/corroboration) must treat the current floor as
three-valued: an ASSERTED open head is the live value; absent one, a NEGATED
open head is the live retraction shown explicitly; an irrealis open head
(hypothetical/reported/question/expected) is never current. The supersession
guard (slice 1) lives in test_supersession.py; this exercises the read side
directly by seeding rows with explicit assertion/status, so the floor logic is
proved without routing through the whole pipeline.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.canonical import (
    CORROBORATION_THRESHOLD,
    corroboration_count,
    promote_if_corroborated,
    reproject_canonical_name,
)
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NOTE_TIME = datetime(2026, 6, 11, 16, 0, tzinfo=UTC)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(database_url: str) -> AsyncIterator[None]:  # noqa: F811
    admin_url = database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test")
    engine = create_async_engine(admin_url, poolclass=NullPool)
    async with async_sessionmaker(engine)() as s:
        await s.execute(
            text(
                "TRUNCATE app.facts, app.entities, app.entity_mentions, app.entity_aliases,"
                " app.temporal_tokens, app.review_items, app.note_analysis,"
                " app.chunks, app.notes, app.subjects CASCADE"
            )
        )
        await s.commit()
    await engine.dispose()
    yield


# --- seeding ----------------------------------------------------------------


async def seed_note(maker: async_sessionmaker[AsyncSession], *, domain: str = "general") -> str:
    note_id = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at)"
                " VALUES (:i, :c, :d, 'seed', :t)"
            ),
            {"i": str(note_id), "c": str(note_id)[:12], "d": domain, "t": NOTE_TIME},
        )
    return str(note_id)


async def seed_entity(
    maker: async_sessionmaker[AsyncSession],
    *,
    name: str,
    kind: str = "Person",
    status: str = "provisional",
    domain: str = "general",
) -> str:
    entity_id = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                " VALUES (:i, :k, :n, :st, :d)"
            ),
            {"i": str(entity_id), "k": kind, "n": name, "st": status, "d": domain},
        )
    return str(entity_id)


async def seed_fact(
    maker: async_sessionmaker[AsyncSession],
    *,
    entity_id: str,
    note_id: str,
    predicate: str,
    statement: str,
    assertion: str = "asserted",
    status: str = "active",
    valid_to: datetime | None = None,
    value_json: dict | None = None,
    kind: str = "attribute",
    qualifier: str = "",
    domain: str = "general",
) -> None:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, qualifier, kind, statement,"
                " value_json, assertion, reported_at, valid_to, temporal_precision, status,"
                " note_id, extractor, prompt_version, domain_code)"
                " VALUES (:id, :eid, :pred, :q, :kind, :stmt, CAST(:vj AS jsonb), :assertion,"
                " :ts, :vt, 'unknown', :status, :nid, 'test', 'test-v1', :dom)"
            ),
            {
                "id": str(uuid.uuid4()),
                "eid": entity_id,
                "pred": predicate,
                "q": qualifier,
                "kind": kind,
                "stmt": statement,
                "vj": json.dumps(value_json) if value_json is not None else None,
                "assertion": assertion,
                "ts": NOTE_TIME,
                "vt": valid_to,
                "status": status,
                "nid": note_id,
                "dom": domain,
            },
        )


def _group(view: dict, predicate: str) -> dict:
    return next(p for p in view["predicates"] if p["predicate"] == predicate)


# --- entity_view ------------------------------------------------------------


async def test_entity_view_shows_negated_open_with_no_asserted_peer(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A live retraction with nothing positive replacing it ("no longer allergic
    to penicillin") is an active, open, NEGATED fact — it must be the current
    value, not hidden, or the entity reads as if the allergy were forgotten."""
    note = await seed_note(maker)
    e = await seed_entity(maker, name="Me", domain="health")
    await seed_fact(
        maker, entity_id=e, note_id=note, predicate="allergy", qualifier="penicillin",
        statement="no longer allergic to penicillin", assertion="negated", domain="health",
    )  # fmt: skip
    view = await SqlAnalysisRepo(maker).entity_view(OWNER, e)
    assert view is not None
    g = _group(view, "allergy")
    assert g["current"] is not None
    assert g["current"]["assertion"] == "negated"


async def test_entity_view_asserted_head_beats_a_negated_peer(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """When both an asserted and a negated open head share a slot, the asserted
    value floors as current regardless of recency (the safe present truth)."""
    note = await seed_note(maker)
    e = await seed_entity(maker, name="Me", domain="health")
    await seed_fact(
        maker, entity_id=e, note_id=note, predicate="allergy", qualifier="penicillin",
        statement="allergic to penicillin", assertion="asserted", domain="health",
    )  # fmt: skip
    await seed_fact(
        maker, entity_id=e, note_id=note, predicate="allergy", qualifier="penicillin",
        statement="not allergic to penicillin", assertion="negated", domain="health",
    )  # fmt: skip
    view = await SqlAnalysisRepo(maker).entity_view(OWNER, e)
    assert view is not None
    assert _group(view, "allergy")["current"]["assertion"] == "asserted"


async def test_entity_view_hides_an_irrealis_only_head(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A hypothetical "maybe I'll switch to Acme" is not a claim about the
    present, so it never floors as current — the slot has no current value even
    though the fact is active and open."""
    note = await seed_note(maker)
    e = await seed_entity(maker, name="Me")
    await seed_fact(
        maker, entity_id=e, note_id=note, predicate="employer",
        statement="maybe I'll switch to Acme", assertion="hypothetical",
    )  # fmt: skip
    view = await SqlAnalysisRepo(maker).entity_view(OWNER, e)
    assert view is not None
    g = _group(view, "employer")
    assert g["current"] is None
    # The fact is still preserved in history (timeline disclosure), just not current.
    assert len(g["history"]) == 1


# --- note_currency ----------------------------------------------------------


async def test_note_currency_reports_a_negated_head_as_the_current_value(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A superseded note's slot whose live head is a NEGATED retraction shows
    that retraction as the current value, not an empty current."""
    stale = await seed_note(maker)
    fresh = await seed_note(maker)
    e = await seed_entity(maker, name="Me")
    await seed_fact(
        maker, entity_id=e, note_id=stale, predicate="employer",
        statement="works at Acme", status="superseded",
    )  # fmt: skip
    await seed_fact(
        maker, entity_id=e, note_id=fresh, predicate="employer",
        statement="no longer at Acme", assertion="negated",
    )  # fmt: skip
    out = await SqlAnalysisRepo(maker).note_currency(OWNER, [stale])
    assert len(out[stale]) == 1
    entry = out[stale][0]
    assert entry["stale_value"] == "works at Acme"
    assert entry["current_value"] == "no longer at Acme"


async def test_note_currency_hides_an_irrealis_head_from_current_value(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An irrealis head ("maybe Beta") is not the present truth, so a superseded
    slot reports no current value rather than the hypothetical."""
    stale = await seed_note(maker)
    fresh = await seed_note(maker)
    e = await seed_entity(maker, name="Me")
    await seed_fact(
        maker, entity_id=e, note_id=stale, predicate="employer",
        statement="works at Acme", status="superseded",
    )  # fmt: skip
    await seed_fact(
        maker, entity_id=e, note_id=fresh, predicate="employer",
        statement="maybe Beta", assertion="hypothetical",
    )  # fmt: skip
    out = await SqlAnalysisRepo(maker).note_currency(OWNER, [stale])
    entry = out[stale][0]
    assert entry["stale_value"] == "works at Acme"
    assert entry["current_value"] is None


# --- canonical (name projection + corroboration) ----------------------------


async def test_reproject_ignores_a_negated_name_fact(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The canonical_name is a positive present identity claim. A negated name
    (here a `name.preferred`, which would outrank the asserted `name.full` by
    precedence if counted) must be excluded, so the asserted name still wins."""
    note = await seed_note(maker)
    e = await seed_entity(maker, name="Sammy")
    await seed_fact(
        maker, entity_id=e, note_id=note, predicate="name.full",
        statement="full name", value_json={"value": "Celine Hopkins"},
    )  # fmt: skip
    await seed_fact(
        maker, entity_id=e, note_id=note, predicate="name.preferred",
        statement="not called Sam", assertion="negated", value_json={"value": "Sam"},
    )  # fmt: skip
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await reproject_canonical_name(s, uuid.UUID(e)) == "Celine Hopkins"


async def test_reproject_does_not_project_a_negated_only_name(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An entity whose only name fact is negated has no positive name to
    project, so the existing canonical_name is left untouched."""
    note = await seed_note(maker)
    e = await seed_entity(maker, name="Sammy")
    await seed_fact(
        maker, entity_id=e, note_id=note, predicate="name.full",
        statement="not Bob", assertion="negated", value_json={"value": "Bob"},
    )  # fmt: skip
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await reproject_canonical_name(s, uuid.UUID(e)) is None
        name = (
            await s.execute(
                text("SELECT canonical_name FROM app.entities WHERE id = :i"), {"i": e}
            )
        ).scalar_one()
    assert name == "Sammy"


async def test_corroboration_counts_only_asserted_facts(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Auto-confirm rests on firm positive evidence: negated and irrealis facts
    do not count toward corroboration (a mention would, but none is seeded), so
    they alone never promote a provisional entity. Asserted facts do count."""
    e = await seed_entity(maker, name="Marcus")
    for assertion in ("negated", "hypothetical", "reported", "question"):
        for _ in range(CORROBORATION_THRESHOLD):
            note = await seed_note(maker)
            await seed_fact(
                maker, entity_id=e, note_id=note, predicate="note",
                statement=f"{assertion} ref", assertion=assertion,
            )  # fmt: skip
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await corroboration_count(s, uuid.UUID(e), "general") == 0
        assert (await promote_if_corroborated(s, uuid.UUID(e))).action == "none"

    for _ in range(CORROBORATION_THRESHOLD):
        note = await seed_note(maker)
        await seed_fact(
            maker, entity_id=e, note_id=note, predicate="note", statement="asserted ref"
        )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await corroboration_count(s, uuid.UUID(e), "general") == CORROBORATION_THRESHOLD
        assert (await promote_if_corroborated(s, uuid.UUID(e))).action == "confirmed"
