"""Migration 0033 backfills value_json onto pre-existing low_confidence_inference
cards, against real Postgres. Runs the migration's own BACKFILL_SQL over seeded
pre-migration rows: a card linking a held fact gains the fact's value_json, a
card with no fact_id is left on its statement floor, and a card that already
carries value_json is not clobbered.
"""

import importlib.util
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _backfill_sql() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0033_backfill_inference_value_json.py"
    )
    spec = importlib.util.spec_from_file_location("mig0033", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BACKFILL_SQL


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_card(maker: async_sessionmaker[AsyncSession], payload: dict[str, Any]) -> str:
    iid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                " VALUES (:id, 'low_confidence_inference', cast(:payload AS jsonb), 'general')"
            ),
            {"id": iid, "payload": json.dumps(payload)},
        )
    return iid


async def _payload(maker: async_sessionmaker[AsyncSession], iid: str) -> dict[str, Any]:
    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text("SELECT payload FROM app.review_items WHERE id = :id"), {"id": iid}
            )
        ).one()
    return row.payload


async def test_backfill_copies_value_from_the_linked_fact(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # An entity + held fact carrying the structured value the analysis page shows.
    eid = str(uuid.uuid4())
    nid = str(uuid.uuid4())
    fid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:id, 'Person', 'Me', 'general')"
            ),
            {"id": eid},
        )
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'general', 'People call me Jeff.')"
            ),
            {"id": nid, "cid": f"bf-{nid[:13]}"},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, value_json,"
                " assertion, reported_at, status, note_id, extractor, prompt_version, domain_code)"
                " VALUES (:id, :eid, 'name.nickname', 'attribute', 'People call me Jeff.',"
                " cast(:vj AS jsonb), 'asserted', now(), 'pending_review', :nid, 'fake', 'v1',"
                " 'general')"
            ),
            {"id": fid, "eid": eid, "vj": json.dumps({"name": "Jeff"}), "nid": nid},
        )

    # Three pre-migration cards: linked-and-missing, no fact_id, already-present.
    linked = await _seed_card(
        maker,
        {"statement": "People call me Jeff.", "predicate": "name.nickname", "fact_id": fid},
    )
    unlinked = await _seed_card(
        maker, {"statement": "People call me Jeff.", "predicate": "name.nickname", "fact_id": None}
    )
    already = await _seed_card(
        maker,
        {
            "statement": "People call me Jeff.",
            "predicate": "name.nickname",
            "fact_id": fid,
            "value_json": {"name": "Keep"},
        },
    )

    async with scoped_session(maker, OWNER) as s:
        await s.execute(text(_backfill_sql()))

    # The linked card gains the fact's value; the renderer now shows "Jeff".
    assert (await _payload(maker, linked))["value_json"] == {"name": "Jeff"}
    # No fact to read from → left on its statement floor, untouched.
    assert "value_json" not in await _payload(maker, unlinked)
    # An existing value is never clobbered.
    assert (await _payload(maker, already))["value_json"] == {"name": "Keep"}
