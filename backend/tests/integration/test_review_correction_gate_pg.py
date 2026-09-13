"""POST /api/review/{id}/correction refuses an UNCORRECTABLE card, server-side, against
real Postgres.

`correctable: false` on a card's payload drops the detail footer's *correct it* composer
in the UI — but that is a rendering hint, and the endpoint is reachable by anything that
is not the shipped UI. The one card that sets the flag is the EMR Layer-2 location
firewall's (`docs/plans/EMR_IMPORT_PLAN.md` §3.6): it exists BECAUSE an address was held
out of the health domain, it sits in that same domain, and a correction note lands there
force-superseded and pinned at full weight. So a client-only guard on that card would
leave the endpoint offering to re-plant exactly the leak the control caught.

The default is the load-bearing half: `correctable` is absent from every other card's
payload and must keep meaning "correctable", mirroring the frontend's
`payload.correctable !== false` — only an explicit `false` refuses.

Fully synchronous (TestClient + asyncio.run for direct-DB work): a sync TestClient call
inside an async test would block the running loop the client's portal also drives.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Iterator, Sequence
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.emr.firewall import FIREWALL_REVIEW_KIND
from jbrain.ingest.emr.importer import FirewallCatch
from jbrain.ingest.emr.integrate import file_firewall_cards
from jbrain.main import create_app
from jbrain.models.analysis import ReviewItem
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_T = TypeVar("_T")


def _run(coro: Awaitable[_T]) -> _T:
    """Drive an async helper to completion from a sync test (no running loop here)."""
    return asyncio.run(coro)  # type: ignore[arg-type]


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


# Every helper opens (and disposes) its OWN engine inside one asyncio.run: asyncpg
# connections are bound to the loop that created them, so a shared engine reused across
# separate asyncio.run() calls would raise "attached to a different loop".
async def _seed(url: str) -> dict[str, str]:
    """Three real review rows: the firewall card built by the shipped filer (never a
    hand-copied payload — the test must break if the filer stops setting the flag), an
    ordinary card with no `correctable` key, and one that sets it explicitly true."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        ctx = await _owner_ctx(maker)
        note_id = uuid.uuid4()
        filed = await file_firewall_cards(
            maker,
            ctx,
            note_id=note_id,
            note_domain="health",
            catches=[
                (
                    str(uuid.uuid4()),
                    FirewallCatch(entity_kind="Encounter", predicate="address", anchor="page 1"),
                )
            ],
        )
        assert filed == 1
        async with scoped_session(maker, ctx) as s:
            firewall = (
                await s.execute(
                    text(
                        "SELECT id::text FROM app.review_items WHERE kind = :k"
                        " AND payload->>'note_id' = :n"
                    ),
                    {"k": FIREWALL_REVIEW_KIND, "n": str(note_id)},
                )
            ).scalar_one()
            plain = ReviewItem(
                kind="low_confidence",
                payload={"summary": "an ordinary card, silent on correctability"},
                domain_code="health",
            )
            explicit = ReviewItem(
                kind="low_confidence",
                payload={"summary": "explicitly correctable", "correctable": True},
                domain_code="general",
            )
            s.add_all([plain, explicit])
            await s.flush()
            return {"firewall": firewall, "plain": str(plain.id), "explicit": str(explicit.id)}
    finally:
        await engine.dispose()


async def _correction_notes(url: str, item_id: str) -> Sequence[Any]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with scoped_session(maker, await _owner_ctx(maker)) as s:
            return (
                await s.execute(
                    text(
                        "SELECT id::text, provenance, domain_code, body FROM app.notes"
                        " WHERE source_ref = :r"
                    ),
                    {"r": f"review:{item_id}"},
                )
            ).all()
    finally:
        await engine.dispose()


async def _rotate_owner_key(url: str) -> str:
    """Rotate the (global) owner key on a FRESH engine. Never `app.state.auth_repo`, whose
    engine is bound to the lifespan thread's loop."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        return await service.rotate_owner_key(SqlAuthRepo(async_sessionmaker(engine)))
    finally:
        await engine.dispose()


@pytest.fixture
def api(database_url: str) -> Iterator[tuple[TestClient, dict[str, str], str]]:  # noqa: F811
    ids = _run(_seed(database_url))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        key = _run(_rotate_owner_key(database_url))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "it"}
            ).status_code
            == 204
        )
        yield client, ids, database_url


# The held address, spelled the way an owner would type it into the composer the card
# refuses to offer — the exact string that must never reach app.notes from this path.
_LEAK = "The address for that encounter is 12 Elm Street."


def test_correction_against_a_firewall_card_is_refused(
    api: tuple[TestClient, dict[str, str], str],
) -> None:
    client, ids, url = api
    resp = client.post(
        f"/api/review/{ids['firewall']}/correction", json={"body": _LEAK, "domain": "health"}
    )
    assert resp.status_code == 409
    # The refusal names why, so a caller is not left guessing at a bare status.
    assert "not correctable" in resp.json()["detail"]
    # And nothing was written: no owner_correction note to force-supersede + pin the held
    # value back into the health domain the guard kept it out of.
    assert _run(_correction_notes(url, ids["firewall"])) == []


def test_correction_against_a_card_with_no_correctable_key_still_works(
    api: tuple[TestClient, dict[str, str], str],
) -> None:
    # The load-bearing default: absent means correctable, for every card in the system.
    client, ids, url = api
    resp = client.post(
        f"/api/review/{ids['plain']}/correction",
        json={"body": "The reading was 120/80, not 210/80.", "domain": "health"},
    )
    assert resp.status_code == 201
    rows = _run(_correction_notes(url, ids["plain"]))
    assert [(r.id, r.provenance, r.domain_code) for r in rows] == [
        (resp.json()["note_id"], "owner_correction", "health")
    ]


def test_correction_against_an_explicitly_correctable_card_still_works(
    api: tuple[TestClient, dict[str, str], str],
) -> None:
    client, ids, url = api
    resp = client.post(
        f"/api/review/{ids['explicit']}/correction",
        json={"body": "Globex is in tech, not retail.", "domain": "general"},
    )
    assert resp.status_code == 201
    rows = _run(_correction_notes(url, ids["explicit"]))
    assert [r.provenance for r in rows] == ["owner_correction"]


async def test_review_correctable_reads_the_payload_on_the_callers_scoped_session(
    database_url: str,  # noqa: F811
) -> None:
    """The gate itself, one level below the endpoint: the three payload shapes plus the
    two ids that name no card, read on the caller's own RLS-scoped session (rule 3)."""
    ids = await _seed(database_url)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        repo = SqlAnalysisRepo(maker)
        ctx = await _owner_ctx(maker)
        assert await repo.review_correctable(ctx, ids["firewall"]) is False
        assert await repo.review_correctable(ctx, ids["plain"]) is True
        assert await repo.review_correctable(ctx, ids["explicit"]) is True
        assert await repo.review_correctable(ctx, str(uuid.uuid4())) is True
        assert await repo.review_correctable(ctx, "item-42") is True
        # A principal that cannot see the health card cannot be refused by it either —
        # the gate reads what the caller reads, and never a widened session.
        general = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
        assert await repo.review_correctable(general, ids["firewall"]) is True
        assert await repo.review_correctable(general, ids["explicit"]) is True
    finally:
        await engine.dispose()


def test_an_id_that_names_no_card_is_not_turned_into_a_gate_failure(
    api: tuple[TestClient, dict[str, str], str],
) -> None:
    # The gate is a leak check, not an existence check: an unknown (and a non-uuid) id
    # keeps the endpoint's prior behaviour rather than becoming a new refusal.
    client, _ids, _url = api
    for item_id in (str(uuid.uuid4()), "item-42"):
        resp = client.post(
            f"/api/review/{item_id}/correction", json={"body": "x", "domain": "general"}
        )
        assert resp.status_code == 201
