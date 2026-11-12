"""Migration 0257 against real Postgres: GrowBot's personas, checkpoints, phones and the body's
rider are owner-only, and the hand-over rules hold where only the database can prove them.

The mandatory RLS isolation test for the four new tables (CLAUDE.md rule 3), then the rules
(docs/plans/GROWBOT_PLAN.md §2c): a phone holds one persona, a persona is on one phone, the
body has one rider; a phone pushes only the persona it holds and only against the version it
pulled; and the owner's routes, end to end over the real repo, checkpoint before every write.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.api import growbot, growbot_personas
from jbrain.api.deps import current_debug_principal, owner_only
from jbrain.api.internal_auth import require_growbot
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, device_context, jshare_context, scoped_session
from jbrain.growbot import prefs, souls
from jbrain.growbot.personas import AUTO_KEEP, Pulled, SqlPersonaRepo
from jbrain.growbot.repo import Redeemed, SqlGrowbotRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SYSTEM_OWNER = SessionContext(principal_kind="owner")
NON_OWNERS = [
    UNSCOPED,
    SessionContext(principal_kind="capability_token", domain_scopes=("general",)),
    SessionContext(principal_kind="member", principal_id="m1", domain_scopes=("general",)),
    device_context("d1", "s1"),
    jshare_context("slug"),
    SessionContext(auth_context="login"),
]
TABLES = ("growbot_phones", "growbot_personas", "growbot_checkpoints", "growbot_body")
PHONE_A = "phone-aaaaaaaaaaaaaaaa"
PHONE_B = "phone-bbbbbbbbbbbbbbbb"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    made = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(made, SYSTEM_OWNER) as session:
        for table in ("growbot_tickets", *reversed(TABLES), "growbot_robots"):
            await session.execute(text(f"DELETE FROM app.{table}"))
    yield made
    await engine.dispose()


async def _count(maker: async_sessionmaker, ctx: SessionContext, table: str) -> int:
    async with scoped_session(maker, ctx) as session:
        return (await session.execute(text(f"SELECT count(*) FROM app.{table}"))).scalar_one()


async def _world(maker: async_sessionmaker) -> tuple[SqlPersonaRepo, str, str]:
    """Fish, and two personas: Sprout on phone A wearing Fish, Bolt not open."""
    await SqlGrowbotRepo(maker).put_robot(OWNER, "gb-fish", "Fish")
    repo = SqlPersonaRepo(maker)
    sprout = (await repo.create(OWNER, "Sprout", souls.fresh("Sprout"), "a fresh creature")).id
    bolt = (await repo.create(OWNER, "Bolt", souls.fresh("Bolt"), "a fresh creature")).id
    assert await repo.open_on_phone(SYSTEM_OWNER, sprout, PHONE_A, "Pixel 6a", "gb-fish")
    return repo, sprout, bolt


# -- isolation ------------------------------------------------------------------------------


async def test_all_four_tables_are_owner_only(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    await repo.save_checkpoint(OWNER, sprout, "First week")
    for table in TABLES:
        assert await _count(maker, OWNER, table) >= 1, table
        for ctx in NON_OWNERS:
            assert await _count(maker, ctx, table) == 0, (table, ctx)


@pytest.mark.parametrize("ctx", NON_OWNERS)
async def test_a_non_owner_can_neither_read_nor_change_a_persona(
    maker: async_sessionmaker, ctx: SessionContext
) -> None:
    repo, sprout, bolt = await _world(maker)
    assert await repo.list_personas(ctx) == []
    assert await repo.get_persona(ctx, sprout) is None
    assert await repo.pull(ctx, sprout, PHONE_A) == "gone"
    assert await repo.push(ctx, sprout, PHONE_A, 1, {"soul_format": 3}) == "gone"
    assert await repo.write_soul(ctx, sprout, {"soul_format": 3, "x": 1}) is None
    assert not await repo.delete(ctx, sprout)
    assert not await repo.set_rider(ctx, "gb-fish", bolt)
    assert not await repo.open_on_phone(ctx, bolt, PHONE_B, "", "gb-fish")
    assert await repo.save_checkpoint(ctx, sprout, "x") is None
    mine = await repo.save_checkpoint(OWNER, sprout, "mine")
    assert mine is not None
    assert not await repo.delete_checkpoint(ctx, sprout, mine.id)
    assert await repo.delete_automatic(ctx, sprout) is None
    assert await repo.list_phones(ctx) == [] and await repo.riders(ctx) == []
    with pytest.raises(DBAPIError):
        await repo.create(ctx, "Evil", {"soul_format": 3}, "")
    # Nothing moved.
    (fish,) = await repo.riders(OWNER)
    assert fish.persona_id == sprout
    got = await repo.get_persona(OWNER, sprout)
    assert got is not None and got.version == 1 and got.phone_id == PHONE_A


# -- the hand-over rules --------------------------------------------------------------------


async def test_a_phone_holds_one_persona_and_a_persona_one_phone(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, bolt = await _world(maker)
    # Bolt opens on phone A, alone: Sprout closes there and is kept on the box.
    assert await repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_A, "", "")
    by = {p.id: p for p in await repo.list_personas(OWNER)}
    assert by[bolt].phone_id == PHONE_A and by[sprout].phone_id is None
    assert by[sprout].last_phone_id == PHONE_A
    # Sprout still wears Fish, from no phone: Fish stands still until a persona connects.
    assert by[sprout].wearing == "gb-fish"
    # Bolt moves to phone B: phone A holds nothing now.
    assert await repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_B, "Tablet", "")
    by = {p.id: p for p in await repo.list_personas(OWNER)}
    assert by[bolt].phone_id == PHONE_B
    assert {f.id: f.label for f in await repo.list_phones(OWNER)} == {
        PHONE_A: "Pixel 6a",
        PHONE_B: "Tablet",
    }
    # The schema itself refuses two personas on one phone.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as session:
            await session.execute(
                text("UPDATE app.growbot_personas SET phone_id = :ph"), {"ph": PHONE_B}
            )


async def test_the_body_has_one_rider(maker: async_sessionmaker) -> None:
    repo, sprout, bolt = await _world(maker)
    # Bolt opens on phone B wearing Fish: Sprout lets go and carries on alone on phone A.
    assert await repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_B, "", "gb-fish")
    by = {p.id: p for p in await repo.list_personas(OWNER)}
    assert by[bolt].wearing == "gb-fish" and by[sprout].wearing is None
    assert by[sprout].phone_id == PHONE_A
    # From the Body tab: Sprout, open on phone A, takes the legs back without phone A acting.
    assert await repo.set_rider(OWNER, "gb-fish", sprout)
    (fish,) = await repo.riders(OWNER)
    assert fish.persona_id == sprout
    assert await repo.set_rider(OWNER, "gb-fish", None)
    assert (await repo.riders(OWNER))[0].persona_id is None
    assert not await repo.set_rider(OWNER, "gb-none", sprout)
    assert not await repo.set_rider(OWNER, "gb-fish", "00000000-0000-4000-8000-000000000000")
    assert not await repo.set_rider(OWNER, "gb-fish", "not-a-uuid")


async def test_opening_alone_on_another_phone_leaves_the_body(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, _ = await _world(maker)
    assert await repo.open_on_phone(SYSTEM_OWNER, sprout, PHONE_B, "", "")
    got = await repo.get_persona(OWNER, sprout)
    assert got is not None and got.wearing is None and got.phone_id == PHONE_B
    # Opened alone on the phone it is already on, it keeps the legs.
    assert await repo.open_on_phone(SYSTEM_OWNER, sprout, PHONE_B, "", "gb-fish")
    assert await repo.open_on_phone(SYSTEM_OWNER, sprout, PHONE_B, "", "")
    got = await repo.get_persona(OWNER, sprout)
    assert got is not None and got.wearing == "gb-fish"


async def test_open_refuses_a_missing_persona_or_robot(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    assert not await repo.open_on_phone(
        SYSTEM_OWNER, "00000000-0000-4000-8000-000000000000", PHONE_B, "", ""
    )
    assert not await repo.open_on_phone(SYSTEM_OWNER, "x", PHONE_B, "", "")
    assert not await repo.open_on_phone(SYSTEM_OWNER, sprout, PHONE_B, "", "gb-gone")
    # A refused open changes nothing: Sprout stays on phone A, wearing Fish, and phone B was
    # never recorded.
    got = await repo.get_persona(OWNER, sprout)
    assert got is not None and got.phone_id == PHONE_A and got.wearing == "gb-fish"
    assert [f.id for f in await repo.list_phones(OWNER)] == [PHONE_A]


async def test_racing_picks_put_a_persona_in_one_body(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    await SqlGrowbotRepo(maker).put_robot(OWNER, "gb-dog", "Dog")
    await repo.set_rider(OWNER, "gb-fish", None)
    picks = await asyncio.gather(
        *(repo.set_rider(OWNER, code, sprout) for code in ("gb-fish", "gb-dog") * 3)
    )
    assert any(picks)
    worn = [r.code for r in await repo.riders(OWNER) if r.persona_id == sprout]
    assert len(worn) == 1


async def test_racing_opens_on_one_phone_hold_one_persona(maker: async_sessionmaker) -> None:
    repo, sprout, bolt = await _world(maker)
    opens = await asyncio.gather(
        *(repo.open_on_phone(SYSTEM_OWNER, p, PHONE_B, "", "") for p in (sprout, bolt) * 3)
    )
    assert any(opens)
    on_b = [p.id for p in await repo.list_personas(OWNER) if p.phone_id == PHONE_B]
    assert len(on_b) == 1


async def test_two_phones_swapping_personas_at_once_both_land(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, bolt = await _world(maker)
    assert await repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_B, "", "")
    for _ in range(5):
        a, b = await asyncio.gather(
            repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_A, "", ""),
            repo.open_on_phone(SYSTEM_OWNER, sprout, PHONE_B, "", ""),
        )
        assert a and b
        held = {p.id: p.phone_id for p in await repo.list_personas(OWNER)}
        assert held == {bolt: PHONE_A, sprout: PHONE_B}
        sprout, bolt = bolt, sprout


async def test_an_open_racing_a_who_wears_pick_never_errors(maker: async_sessionmaker) -> None:
    repo, sprout, bolt = await _world(maker)
    for _ in range(5):
        opened, picked = await asyncio.gather(
            repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_B, "", "gb-fish"),
            repo.set_rider(OWNER, "gb-fish", sprout),
        )
        assert opened and picked
        (rider,) = await repo.riders(OWNER)
        assert rider.persona_id in (sprout, bolt)


async def test_an_open_racing_the_held_personas_push_never_errors(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, bolt = await _world(maker)
    for n in range(5):
        pulled = await repo.pull(SYSTEM_OWNER, sprout, PHONE_A)
        assert isinstance(pulled, Pulled)
        opened, _ = await asyncio.gather(
            repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_A, "", ""),
            repo.push(SYSTEM_OWNER, sprout, PHONE_A, pulled.version, {"soul_format": 3, "n": n}),
        )
        assert opened
        assert await repo.open_on_phone(SYSTEM_OWNER, sprout, PHONE_A, "", "")


async def test_a_pick_racing_a_delete_never_errors(maker: async_sessionmaker) -> None:
    repo, sprout, bolt = await _world(maker)
    await SqlGrowbotRepo(maker).put_robot(OWNER, "gb-wren", "Wren")
    picked, _ = await asyncio.gather(
        repo.set_rider(OWNER, "gb-wren", sprout), repo.delete(OWNER, sprout)
    )
    assert isinstance(picked, bool)
    assert [p.id for p in await repo.list_personas(OWNER)] == [bolt]


async def test_an_open_refused_after_it_began_writing_rolls_back(
    maker: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sprout, bolt = await _world(maker)
    real = repo._ride

    async def robot_forgotten(*_: object) -> bool:
        return False

    monkeypatch.setattr(repo, "_ride", robot_forgotten)
    assert not await repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_A, "", "gb-fish")
    monkeypatch.setattr(repo, "_ride", real)
    held = {p.id: p.phone_id for p in await repo.list_personas(OWNER)}
    assert held == {sprout: PHONE_A, bolt: None}


async def test_an_edit_from_an_older_read_is_refused(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    assert await repo.push(SYSTEM_OWNER, sprout, PHONE_A, 1, {"soul_format": 3, "n": 1}) == 2
    assert await repo.write_soul(OWNER, sprout, {"soul_format": 3}, expect=1) == "stale"
    got = await repo.get_persona(OWNER, sprout)
    assert got is not None and got.soul == {"soul_format": 3, "n": 1}
    done = await repo.write_soul(OWNER, sprout, {"soul_format": 3}, expect=2)
    assert done is not None and done != "stale" and done[0].version == 3


# -- sync -----------------------------------------------------------------------------------


async def test_a_phone_pushes_only_what_it_holds_against_what_it_pulled(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, _ = await _world(maker)
    pulled = await repo.pull(SYSTEM_OWNER, sprout, PHONE_A)
    assert isinstance(pulled, Pulled) and pulled.version == 1 and pulled.wearing == "gb-fish"
    assert await repo.pull(SYSTEM_OWNER, sprout, PHONE_B) == "moved"
    grown = {"soul_format": 3, "identity": "I am Sprout and I walked six steps today."}
    assert await repo.push(SYSTEM_OWNER, sprout, PHONE_A, 1, grown) == 2
    # A stale base is refused: the phone must pull first.
    assert await repo.push(SYSTEM_OWNER, sprout, PHONE_A, 1, {"soul_format": 3}) == "newer"
    # The owner's edit is a newer soul, so the phone's next push is refused too.
    await repo.write_soul(OWNER, sprout, {"soul_format": 3, "edited": True}, before=("b", "w"))
    assert await repo.push(SYSTEM_OWNER, sprout, PHONE_A, 2, grown) == "newer"
    again = await repo.pull(SYSTEM_OWNER, sprout, PHONE_A)
    assert isinstance(again, Pulled) and again.version == 3 and again.soul["edited"] is True
    # Another phone that never held it is refused as moved, whatever version it names.
    assert await repo.push(SYSTEM_OWNER, sprout, PHONE_B, 3, grown) == "moved"
    assert (
        await repo.push(SYSTEM_OWNER, "00000000-0000-4000-8000-000000000000", PHONE_A, 1, {})
        == "gone"
    )
    assert await repo.push(SYSTEM_OWNER, "nope", PHONE_A, 1, {}) == "gone"
    assert await repo.pull(SYSTEM_OWNER, "nope", PHONE_A) == "gone"


async def test_racing_pushes_on_one_version_write_once(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    results = await asyncio.gather(
        *(repo.push(SYSTEM_OWNER, sprout, PHONE_A, 1, {"soul_format": 3, "n": i}) for i in range(6))
    )
    assert sorted(str(r) for r in results) == ["2"] + ["newer"] * 5


# -- checkpoints ----------------------------------------------------------------------------


async def test_a_write_checkpoints_the_soul_as_it_was(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    done = await repo.write_soul(
        OWNER, sprout, {"soul_format": 3, "v": 2}, name="Sprout II", before=("Before x", "why")
    )
    assert done is not None and done != "stale"
    persona, cp = done
    assert (
        persona.version == 2
        and persona.name == "Sprout II"
        and persona.soul
        == {
            "soul_format": 3,
            "v": 2,
        }
    )
    assert cp is not None and cp.auto and cp.name == "Before x"
    full = await repo.get_checkpoint(OWNER, cp.id)
    assert full is not None and full.soul == souls.fresh("Sprout")
    assert await repo.write_soul(OWNER, "00000000-0000-4000-8000-000000000000", {}) is None
    assert await repo.write_soul(OWNER, "nope", {}) is None


async def test_automatic_checkpoints_are_pruned_and_named_ones_kept(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, _ = await _world(maker)
    await repo.save_checkpoint(OWNER, sprout, "Before the beach trip")
    for i in range(AUTO_KEEP + 3):
        await repo.write_soul(OWNER, sprout, {"soul_format": 3, "i": i}, before=(f"a{i}", ""))
    cps = await repo.list_checkpoints(OWNER, sprout)
    assert sum(c.auto for c in cps) == AUTO_KEEP
    assert [c.name for c in cps if not c.auto] == ["Before the beach trip"]
    # Newest first, and the oldest automatic ones went.
    assert cps[0].name == f"a{AUTO_KEEP + 2}" and "a0" not in {c.name for c in cps}
    assert await repo.list_checkpoints(OWNER, "nope") == []
    assert await repo.get_checkpoint(OWNER, "nope") is None
    assert await repo.save_checkpoint(OWNER, "nope", "x") is None
    assert await repo.save_checkpoint(OWNER, "00000000-0000-4000-8000-000000000000", "x") is None


async def test_one_checkpoint_is_deleted_and_only_from_its_own_persona(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, bolt = await _world(maker)
    named = await repo.save_checkpoint(OWNER, sprout, "Before the beach trip")
    auto = await repo.save_checkpoint(OWNER, sprout, "Before x", auto=True)
    theirs = await repo.save_checkpoint(OWNER, bolt, "Bolt's")
    assert named is not None and auto is not None and theirs is not None
    # Named or automatic alike; never through another persona's id.
    assert not await repo.delete_checkpoint(OWNER, sprout, theirs.id)
    assert await repo.delete_checkpoint(OWNER, sprout, auto.id)
    assert await repo.delete_checkpoint(OWNER, sprout, named.id)
    assert not await repo.delete_checkpoint(OWNER, sprout, named.id)
    assert await repo.list_checkpoints(OWNER, sprout) == []
    assert [c.id for c in await repo.list_checkpoints(OWNER, bolt)] == [theirs.id]
    gone = "00000000-0000-4000-8000-000000000000"
    assert not await repo.delete_checkpoint(OWNER, gone, theirs.id)
    assert not await repo.delete_checkpoint(OWNER, "nope", theirs.id)
    assert not await repo.delete_checkpoint(OWNER, bolt, "nope")
    persona = await repo.get_persona(OWNER, sprout)
    assert persona is not None and persona.checkpoints == 0 and persona.version == 1


async def test_deleting_the_automatic_ones_keeps_the_named(maker: async_sessionmaker) -> None:
    repo, sprout, bolt = await _world(maker)
    await repo.save_checkpoint(OWNER, sprout, "Before the beach trip")
    for i in range(3):
        await repo.write_soul(OWNER, sprout, {"soul_format": 3, "i": i}, before=(f"a{i}", ""))
    await repo.save_checkpoint(OWNER, bolt, "Bolt's", auto=True)
    assert await repo.delete_automatic(OWNER, sprout) == 3
    assert await repo.delete_automatic(OWNER, sprout) == 0
    assert [c.name for c in await repo.list_checkpoints(OWNER, sprout)] == ["Before the beach trip"]
    assert len(await repo.list_checkpoints(OWNER, bolt)) == 1
    assert await repo.delete_automatic(OWNER, "00000000-0000-4000-8000-000000000000") is None
    assert await repo.delete_automatic(OWNER, "nope") is None


async def test_deleting_a_persona_takes_its_checkpoints_and_tickets(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, bolt = await _world(maker)
    await repo.save_checkpoint(OWNER, sprout, "keep?")
    tickets = SqlGrowbotRepo(maker)
    ticket = (await tickets.mint_ticket(OWNER, "", sprout)).ticket
    assert await repo.delete(OWNER, sprout)
    assert not await repo.delete(OWNER, sprout) and not await repo.delete(OWNER, "nope")
    assert await _count(maker, OWNER, "growbot_checkpoints") == 0
    assert await tickets.redeem_ticket(SYSTEM_OWNER, ticket) is None
    # The body it wore stands still.
    assert (await repo.riders(OWNER))[0].persona_id is None
    assert [p.id for p in await repo.list_personas(OWNER)] == [bolt]


async def test_a_ticket_names_its_persona(maker: async_sessionmaker) -> None:
    _, sprout, _ = await _world(maker)
    tickets = SqlGrowbotRepo(maker)
    minted = await tickets.mint_ticket(OWNER, "gb-fish", sprout)
    assert await tickets.redeem_ticket(SYSTEM_OWNER, minted.ticket) == Redeemed("gb-fish", sprout)


async def test_the_schema_bounds_names_and_shapes(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    with pytest.raises(DBAPIError):
        await repo.create(OWNER, "x" * 41, {"soul_format": 3}, "")
    with pytest.raises(DBAPIError):
        await repo.create(OWNER, "", {"soul_format": 3}, "")
    # A phone id the schema refuses opens nothing, and changes nothing.
    assert not await repo.open_on_phone(SYSTEM_OWNER, sprout, "short", "", "")
    got = await repo.get_persona(OWNER, sprout)
    assert got is not None and got.phone_id == PHONE_A
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as session:
            await session.execute(text("UPDATE app.growbot_personas SET soul = '[1]'::jsonb"))
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as session:
            await session.execute(text("UPDATE app.growbot_personas SET face = 'cat'"))
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as session:
            await session.execute(text("UPDATE app.growbot_personas SET voice = 'af_heart'"))


# -- the routes, over the real repo ---------------------------------------------------------

OWNER_P = PrincipalInfo(id="00000000-0000-0000-0000-000000000001", kind="owner", label="owner")


def _client(maker: async_sessionmaker) -> TestClient:
    app = FastAPI()
    for module in (growbot, growbot_personas):
        app.include_router(module.router, prefix="/api")
        app.include_router(module.debug_router, prefix="/api")
        app.include_router(module.internal_router, prefix="/internal")
    app.state.settings = SimpleNamespace(
        lan_addr="https://jbrain.local", jcode_preview_base_host="", site_addr=""
    )
    app.state.growbot_repo = SqlGrowbotRepo(maker)
    app.state.growbot_personas = SqlPersonaRepo(maker)
    app.dependency_overrides[owner_only] = lambda: OWNER_P
    app.dependency_overrides[current_debug_principal] = lambda: OWNER_P
    app.dependency_overrides[require_growbot] = lambda: None
    return TestClient(app)


def _make(c: TestClient, name: str = "Sprout", **extra: Any) -> dict[str, Any]:
    r = c.post("/api/growbot/personas", json={"name": name, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def test_routes_add_edit_undo_and_restore(maker: async_sessionmaker) -> None:
    c = _client(maker)
    sprout = _make(c, "  Sprout  ")
    assert sprout["name"] == "Sprout" and sprout["made_from"] == "a fresh creature"
    pid = sprout["id"]
    # An edit checkpoints first; its checkpoint is the Undo.
    r = c.put(f"/api/growbot/personas/{pid}/regions/persona", json={"value": "You are brave."})
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["persona"]["soul"]["persona"] == {"constitution": "You are brave."}
    assert edited["checkpoint"]["name"] == "Before editing personality"
    assert edited["persona"]["version"] == 2
    bad = c.put(f"/api/growbot/personas/{pid}/regions/persona", json={"value": " "})
    assert bad.status_code == 422 and "Nothing was saved" in bad.json()["detail"]
    assert c.put(f"/api/growbot/personas/{pid}/regions/mood", json={"value": {}}).status_code == 422
    who = c.put(
        f"/api/growbot/personas/{pid}/regions/identity",
        json={"value": "I am Sprout and I live by the window."},
    ).json()
    assert who["persona"]["soul"]["identity"] == "I am Sprout and I live by the window."
    assert who["checkpoint"]["name"] == "Before editing who it is"
    # An editor opened before the creature wrote again is refused, not allowed to drop its lines.
    stale = c.put(
        f"/api/growbot/personas/{pid}/regions/identityCore",
        json={"value": "My person is Jeff.", "base": who["persona"]["version"] - 1},
    )
    assert stale.status_code == 409 and "Nothing was saved" in stale.json()["detail"]
    fresh = c.put(f"/api/growbot/personas/{pid}/regions/log", json={"value": []})
    assert fresh.status_code == 422 and "hasn't written this yet" in fresh.json()["detail"]
    woke = {"soul_format": 3, "meta": {"name": "Fish", "kind": "snapshot"}, "goals": {}}
    fish = c.post("/api/growbot/personas/import", json={"soul": woke}).json()["persona"]
    wants = c.put(
        f"/api/growbot/personas/{fish['id']}/regions/goals",
        json={
            "value": {"selfName": "Fish", "wants": ["walk to my person"]},
            "base": fish["version"],
        },
    )
    assert wants.status_code == 200, wants.text
    assert wants.json()["persona"]["soul"]["goals"]["selfName"] == "Fish"
    undo = c.post(
        f"/api/growbot/personas/{pid}/checkpoints/{edited['checkpoint']['id']}/restore"
    ).json()
    assert "persona" not in undo["persona"]["soul"]
    assert undo["checkpoint"]["name"] == "Before restoring “Before editing personality”"
    # A checkpoint of another persona cannot be restored onto this one.
    other = _make(c, "Bolt")
    wrong = c.post(
        f"/api/growbot/personas/{other['id']}/checkpoints/{edited['checkpoint']['id']}/restore"
    )
    assert wrong.status_code == 404
    # Meta renames the persona too.
    meta = c.put(
        f"/api/growbot/personas/{pid}/regions/meta",
        json={"value": {"name": "Pip", "description": "d"}},
    ).json()
    assert meta["persona"]["name"] == "Pip"
    detail = c.get(f"/api/growbot/personas/{pid}").json()
    assert [cp["auto"] for cp in detail["checkpoint_list"]] == [True, True, True, True]


async def test_routes_seed_duplicate_rename_reset_and_delete(maker: async_sessionmaker) -> None:
    c = _client(maker)
    pid = _make(c)["id"]
    c.put(f"/api/growbot/personas/{pid}/regions/persona", json={"value": "You are Bolt."})
    cp = c.post(f"/api/growbot/personas/{pid}/checkpoints", json={"name": " First week "}).json()
    assert cp["name"] == "First week" and not cp["auto"]
    assert c.post(f"/api/growbot/personas/{pid}/checkpoints", json={"name": " "}).status_code == 422
    nobody = "/api/growbot/personas/00000000-0000-4000-8000-000000000000/checkpoints"
    assert c.post(nobody, json={"name": "x"}).status_code == 404
    # Deleting one makes no checkpoint of its own, and a second delete finds nothing.
    spare = c.post(f"/api/growbot/personas/{pid}/checkpoints", json={"name": "spare"}).json()
    before = c.get(f"/api/growbot/personas/{pid}").json()["checkpoints"]
    gone = f"/api/growbot/personas/{pid}/checkpoints/{spare['id']}"
    assert c.delete(gone).status_code == 204
    assert c.delete(gone).status_code == 404
    assert c.get(f"/api/growbot/personas/{pid}").json()["checkpoints"] == before - 1
    assert c.delete(f"/api/growbot/personas/{pid}/checkpoints/nope").status_code == 422
    autos = c.delete(f"/api/growbot/personas/{pid}/checkpoints/automatic")
    assert autos.status_code == 200 and autos.json()["deleted"] >= 1
    left = c.get(f"/api/growbot/personas/{pid}").json()["checkpoint_list"]
    assert left and not any(cp["auto"] for cp in left)
    gone_p = "/api/growbot/personas/00000000-0000-4000-8000-000000000000/checkpoints/automatic"
    assert c.delete(gone_p).status_code == 404
    seeded = _make(c, "Captain Bolt", seed=cp["id"])
    assert seeded["made_from"] == "Sprout — First week"
    assert seeded["soul"]["persona"] == {"constitution": "You are Bolt."}
    assert seeded["soul"]["meta"]["based_on"] == "Sprout / First week"
    missing = c.post(
        "/api/growbot/personas",
        json={"name": "x", "seed": "00000000-0000-4000-8000-000000000000"},
    )
    assert missing.status_code == 404
    assert c.post("/api/growbot/personas", json={"name": "  "}).status_code == 422

    dup = c.post(f"/api/growbot/personas/{pid}/duplicate").json()
    assert dup["name"] == "Sprout copy" and dup["checkpoint_list"] == []
    assert dup["soul"]["persona"] == {"constitution": "You are Bolt."}
    renamed = c.patch(f"/api/growbot/personas/{dup['id']}", json={"name": "Pip"}).json()
    assert renamed["persona"]["name"] == "Pip" and renamed["checkpoint"] is None
    assert renamed["persona"]["soul"]["meta"]["name"] == "Pip"

    # A face is the persona's own, lantern until picked, and not a soul write (its phone does
    # not reload for it); a duplicate wears its original's.
    assert dup["face"] == "lantern"
    version = renamed["persona"]["version"]
    faced = c.patch(f"/api/growbot/personas/{dup['id']}", json={"face": "robot"}).json()
    assert faced["persona"]["face"] == "robot" and faced["persona"]["version"] == version
    assert faced["persona"]["name"] == "Pip" and faced["checkpoint"] is None
    assert c.patch(f"/api/growbot/personas/{dup['id']}", json={"face": "cat"}).status_code == 422
    nobody = "/api/growbot/personas/00000000-0000-4000-8000-000000000000"
    assert c.patch(nobody, json={"face": "robot"}).status_code == 404
    # A bad name refuses the whole edit, face included; an empty edit is refused too.
    bad = c.patch(f"/api/growbot/personas/{dup['id']}", json={"face": "pixel", "name": "  "})
    assert bad.status_code == 422
    assert c.patch(f"/api/growbot/personas/{dup['id']}", json={}).status_code == 422
    assert c.post(f"/api/growbot/personas/{dup['id']}/duplicate").json()["face"] == "robot"

    # A voice is the persona's own too: the box's default ("") until picked, not a soul write,
    # back to the default with "", and only a tts-stt voice id is one.
    assert dup["voice"] == ""
    voiced = c.patch(f"/api/growbot/personas/{dup['id']}", json={"voice": "kokoro-am_puck"})
    assert voiced.json()["persona"]["voice"] == "kokoro-am_puck"
    assert voiced.json()["persona"]["version"] == version
    assert voiced.json()["persona"]["face"] == "robot"
    copied = c.post(f"/api/growbot/personas/{dup['id']}/duplicate").json()
    assert copied["voice"] == "kokoro-am_puck"
    for bad_voice in ("am_puck", "kokoro-AM", "kokoro-a b", "kokoro-" + "x" * 41):
        refused = c.patch(f"/api/growbot/personas/{dup['id']}", json={"voice": bad_voice})
        assert refused.status_code == 422
    assert c.patch(nobody, json={"voice": "kokoro-am_puck"}).status_code == 404
    cleared = c.patch(f"/api/growbot/personas/{dup['id']}", json={"voice": ""}).json()
    assert cleared["persona"]["voice"] == ""

    # Exploring alone is on until the owner turns it off; not a soul write, and a copy keeps it.
    assert dup["explore"] is True
    calm = c.patch(f"/api/growbot/personas/{dup['id']}", json={"explore": False}).json()
    assert calm["persona"]["explore"] is False and calm["persona"]["version"] == version
    assert c.post(f"/api/growbot/personas/{dup['id']}/duplicate").json()["explore"] is False
    assert c.patch(nobody, json={"explore": True}).status_code == 404
    assert c.patch(f"/api/growbot/personas/{dup['id']}", json={"explore": "x"}).status_code == 422

    reset = c.post(f"/api/growbot/personas/{pid}/reset").json()
    assert reset["persona"]["soul"] == souls.fresh("Sprout")
    assert reset["checkpoint"]["name"] == "Before reset"

    assert c.delete(f"/api/growbot/personas/{pid}").status_code == 204
    assert c.delete(f"/api/growbot/personas/{pid}").status_code == 404
    assert c.get(f"/api/growbot/personas/{pid}").status_code == 404
    assert c.get("/api/growbot/personas/not-a-uuid").status_code == 422


async def test_routes_export_and_import(maker: async_sessionmaker) -> None:
    c = _client(maker)
    pid = _make(c, "Captain Bolt")["id"]
    r = c.get(f"/api/growbot/personas/{pid}/export")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="captain-bolt-soul.json"'
    doc = r.json()
    assert doc["meta"]["kind"] == "snapshot" and doc["soul_format"] == 3

    other = _make(c, "火の鳥 Pip")["id"]
    named_header = c.get(f"/api/growbot/personas/{other}/export").headers
    assert named_header["content-disposition"] == 'attachment; filename="pip-soul.json"'
    made = c.post("/api/growbot/personas/import", json={"soul": doc}).json()
    assert (
        made["persona"]["name"] == "Captain Bolt" and made["persona"]["made_from"] == "a soul file"
    )
    named = c.post("/api/growbot/personas/import", json={"soul": doc, "name": "Bolt 2"}).json()
    assert named["persona"]["name"] == "Bolt 2"
    over = c.post(
        "/api/growbot/personas/import",
        json={"soul": doc | {"identity": "I am Bolt and I sail the desk."}, "over": pid},
    ).json()
    assert over["checkpoint"]["name"] == "Before import"
    assert over["persona"]["soul"]["identity"] == "I am Bolt and I sail the desk."
    for bad in ({"soul": "x"}, {"soul": {"soul_format": 2}}):
        r = c.post("/api/growbot/personas/import", json=bad)
        assert r.status_code == 422 and "Nothing changed" in r.json()["detail"]
    gone = c.post(
        "/api/growbot/personas/import",
        json={"soul": doc, "over": "00000000-0000-4000-8000-000000000000"},
    )
    assert gone.status_code == 404


async def test_routes_open_hand_over_and_who_wears_fish(maker: async_sessionmaker) -> None:
    await SqlGrowbotRepo(maker).put_robot(OWNER, "gb-fish", "Fish")
    c = _client(maker)
    sprout, bolt = _make(c)["id"], _make(c, "Bolt")["id"]
    redeem = "/internal/growbot/ticket/redeem"

    def open_on(pid: str, phone: str, code: str | None = None, label: str = "") -> dict:
        body: dict[str, Any] = {"persona": pid}
        if code:
            body["code"] = code
        ticket = c.post("/api/growbot/ticket", json=body)
        assert ticket.status_code == 200, ticket.text
        r = c.post(redeem, json={"ticket": ticket.json()["ticket"], "phone": phone, "label": label})
        assert r.status_code == 200, r.text
        return r.json()

    a = open_on(sprout, PHONE_A, "gb-fish", "  Pixel   6a ")
    assert a == {"ok": True, "code": "gb-fish", "persona": sprout, "phone": PHONE_A}
    # A phone with no id of ours gets a new one.
    fresh = open_on(bolt, "x")
    assert fresh["phone"] != "x" and len(fresh["phone"]) == 32

    over = c.get("/api/growbot/personas").json()
    assert {f["id"]: f["label"] for f in over["phones"]}[PHONE_A] == "Pixel 6a"
    assert over["body"] == [
        {"code": "gb-fish", "persona": sprout, "since": over["body"][0]["since"]}
    ]
    assert c.get("/internal/growbot/body/gb-fish").json() == {"persona": sprout, "phone": PHONE_A}

    # Who wears Fish, from the Body tab: Bolt, open on the other phone, takes the legs there.
    moved = c.put("/api/growbot/body/gb-fish", json={"persona": bolt}).json()
    assert moved["body"][0]["persona"] == bolt
    assert c.get("/internal/growbot/body/gb-fish").json() == {
        "persona": bolt,
        "phone": fresh["phone"],
    }
    assert (
        c.put("/api/growbot/body/gb-fish", json={"persona": None}).json()["body"][0]["persona"]
        is None
    )
    assert c.get("/internal/growbot/body/gb-fish").json() == {"persona": None, "phone": None}
    assert c.get("/internal/growbot/body/gb-none").json() == {"persona": None, "phone": None}
    assert c.put("/api/growbot/body/gb-none", json={"persona": bolt}).status_code == 404
    assert c.put("/api/growbot/body/x", json={}).status_code == 422

    # Sync over the sidecar's routes.
    pulled = c.post("/internal/growbot/soul/pull", json={"persona": sprout, "phone": PHONE_A})
    assert pulled.status_code == 200 and pulled.json()["version"] == 1
    assert pulled.json()["wearing"] is None and pulled.json()["face"] == "lantern"
    assert pulled.json()["voice"] == "" and pulled.json()["explore"] is True
    pushed = c.post(
        "/internal/growbot/soul/push",
        json={"persona": sprout, "phone": PHONE_A, "base": 1, "soul": {"soul_format": 3, "n": 1}},
    )
    assert pushed.json() == {"version": 2}
    stale = c.post(
        "/internal/growbot/soul/push",
        json={"persona": sprout, "phone": PHONE_A, "base": 1, "soul": {"soul_format": 3}},
    )
    assert stale.status_code == 409 and stale.json()["detail"] == "newer"
    state = c.post("/internal/growbot/soul/state", json={"persona": sprout, "phone": PHONE_A})
    assert state.json() == {
        "version": 2,
        "wearing": None,
        "memories": 0,
        "face": "lantern",
        "voice": "",
        "prefs": 0,
        "explore": True,
    }
    # The owner's pick reaches the phone at its next poll, without a soul write.
    assert c.patch(f"/api/growbot/personas/{sprout}", json={"voice": "kokoro-bm_fable"}).is_success
    state = c.post("/internal/growbot/soul/state", json={"persona": sprout, "phone": PHONE_A})
    assert state.json()["voice"] == "kokoro-bm_fable" and state.json()["version"] == 2
    assert c.patch(f"/api/growbot/personas/{sprout}", json={"explore": False}).is_success
    state = c.post("/internal/growbot/soul/state", json={"persona": sprout, "phone": PHONE_A})
    assert state.json()["explore"] is False and state.json()["version"] == 2
    moved_state = c.post("/internal/growbot/soul/state", json={"persona": sprout, "phone": PHONE_B})
    assert moved_state.status_code == 409
    assert c.post("/internal/growbot/soul/state", json={}).status_code == 401
    elsewhere = c.post("/internal/growbot/soul/pull", json={"persona": sprout, "phone": PHONE_B})
    assert elsewhere.status_code == 409 and elsewhere.json()["detail"] == "moved"
    for body in (
        {"persona": sprout, "phone": "short"},
        {},
        {"persona": "x" * 65, "phone": PHONE_A},
    ):
        assert c.post("/internal/growbot/soul/pull", json=body).status_code == 401
        assert c.post("/internal/growbot/soul/push", json=body).status_code == 401
    not_soul = c.post(
        "/internal/growbot/soul/push",
        json={"persona": sprout, "phone": PHONE_A, "base": 2, "soul": {"soul_format": 2}},
    )
    assert not_soul.status_code == 400
    huge = {"soul_format": 3, "log": ["x" * 1000] * 1100}
    too_big = c.post(
        "/internal/growbot/soul/push",
        json={"persona": sprout, "phone": PHONE_A, "base": 2, "soul": huge},
    )
    assert too_big.status_code == 413
    gone = c.post(
        "/internal/growbot/soul/push",
        json={
            "persona": "00000000-0000-4000-8000-000000000000",
            "phone": PHONE_A,
            "base": 1,
            "soul": {"soul_format": 3},
        },
    )
    assert gone.status_code == 404

    # A ticket for a deleted persona opens nothing.
    ticket = c.post("/api/growbot/ticket", json={"persona": bolt}).json()["ticket"]
    c.delete(f"/api/growbot/personas/{bolt}")
    assert c.post(redeem, json={"ticket": ticket, "phone": PHONE_B}).status_code == 401
    gone_pull = c.post(
        "/internal/growbot/soul/pull", json={"persona": bolt, "phone": fresh["phone"]}
    )
    assert gone_pull.status_code == 404 and gone_pull.json()["detail"] == "gone"
    unknown = c.post(
        "/api/growbot/ticket", json={"persona": "00000000-0000-4000-8000-000000000000"}
    )
    assert unknown.status_code == 404


async def test_debug_routes_read_save_and_restore(maker: async_sessionmaker) -> None:
    c = _client(maker)
    pid = _make(c)["id"]
    assert [p["id"] for p in c.get("/api/debug/growbot/personas").json()["personas"]] == [pid]
    assert c.get(f"/api/debug/growbot/personas/{pid}").json()["soul"] == souls.fresh("Sprout")
    cp = c.post(f"/api/debug/growbot/personas/{pid}/checkpoints", json={"name": "safe"}).json()
    c.put(f"/api/growbot/personas/{pid}/regions/persona", json={"value": "You are lost."})
    back = c.post(f"/api/debug/growbot/personas/{pid}/checkpoints/{cp['id']}/restore").json()
    assert back["persona"]["soul"] == souls.fresh("Sprout")
    assert (
        c.post(f"/api/debug/growbot/personas/{pid}/checkpoints", json={"name": ""}).status_code
        == 422
    )


# -- settings (migration 0263) --------------------------------------------------------------


async def test_a_phone_keeps_its_personas_settings_and_the_owner_edits_them(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, _ = await _world(maker)
    # Nothing yet: the first phone sends its own.
    assert await repo.phone_prefs(SYSTEM_OWNER, sprout, PHONE_A) == (0, None, False)
    seed = prefs.patch_from({"gb_vol": "0.5", "gb_eye": "environment", "gb_nx_sid": "nx-a1"}, [])
    assert await repo.phone_patch_prefs(SYSTEM_OWNER, sprout, PHONE_A, seed) == 1
    # The owner's edit and the creature's change to another key both land.
    assert await repo.patch_prefs(OWNER, sprout, prefs.patch_from(None, ["gb_eye"])) == 2
    mine = prefs.patch_from({"gb_face_color": "#ffffff"}, [])
    assert await repo.phone_patch_prefs(SYSTEM_OWNER, sprout, PHONE_A, mine) == 3
    assert await repo.phone_prefs(SYSTEM_OWNER, sprout, PHONE_A) == (
        3,
        {"gb_vol": "0.5", "gb_face_color": "#ffffff", "gb_nx_sid": "nx-a1"},
        False,
    )
    pulled = await repo.pull(SYSTEM_OWNER, sprout, PHONE_A, with_soul=False)
    assert isinstance(pulled, Pulled) and pulled.prefs == 3 and pulled.version == 1
    # Only the phone that holds it, and only a persona that is there.
    assert await repo.phone_prefs(SYSTEM_OWNER, sprout, PHONE_B) == "moved"
    assert await repo.phone_patch_prefs(SYSTEM_OWNER, sprout, PHONE_B, mine) == "moved"
    assert await repo.phone_prefs(SYSTEM_OWNER, "nope", PHONE_A) == "gone"
    assert await repo.phone_patch_prefs(SYSTEM_OWNER, "nope", PHONE_A, mine) == "gone"
    assert await repo.patch_prefs(OWNER, "nope", mine) is None
    missing = "00000000-0000-4000-8000-000000000000"
    assert await repo.patch_prefs(OWNER, missing, mine) is None
    # Past the caps as a whole: nothing written.
    big = prefs.patch_from({f"k{i}": "x" * 15_000 for i in range(5)}, [])
    with pytest.raises(prefs.PrefsTooBig):
        await repo.phone_patch_prefs(SYSTEM_OWNER, sprout, PHONE_A, big)
    got = await repo.phone_prefs(SYSTEM_OWNER, sprout, PHONE_A)
    assert isinstance(got, tuple) and got[0] == 3
    # A duplicate is the same creature, settings and all, but keeps a diary of its own.
    made = await repo.create(OWNER, "Copy", souls.fresh("Copy"), "copy", memories_from=sprout)
    assert made.prefs == {"gb_vol": "0.5", "gb_face_color": "#ffffff"}


async def test_racing_settings_changes_all_land(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    await asyncio.gather(
        *(
            repo.phone_patch_prefs(
                SYSTEM_OWNER, sprout, PHONE_A, prefs.patch_from({f"k{i}": "1"}, [])
            )
            for i in range(6)
        ),
        *(repo.patch_prefs(OWNER, sprout, prefs.patch_from({f"o{i}": "1"}, [])) for i in range(4)),
    )
    got = await repo.phone_prefs(SYSTEM_OWNER, sprout, PHONE_A)
    assert isinstance(got, tuple) and got[0] == 10 and len(got[1] or {}) == 10


async def test_the_schema_holds_settings_to_a_map(maker: async_sessionmaker) -> None:
    repo, sprout, _ = await _world(maker)
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, OWNER) as session:
            await session.execute(
                text("UPDATE app.growbot_personas SET prefs = '[]'::jsonb WHERE id = :p"),
                {"p": sprout},
            )


async def test_routes_read_and_edit_settings(maker: async_sessionmaker) -> None:
    c = _client(maker)
    pid = _make(c)["id"]
    assert c.get(f"/api/growbot/personas/{pid}").json()["prefs"] is None
    r = c.patch(f"/api/growbot/personas/{pid}/prefs", json={"set": {"gb_eye": "environment"}})
    assert r.status_code == 200 and r.json() == {"version": 1}
    r = c.patch(f"/api/debug/growbot/personas/{pid}/prefs", json={"set": {"gb_vol": "0.4"}})
    assert r.status_code == 200 and r.json() == {"version": 2}
    assert c.get(f"/api/growbot/personas/{pid}").json()["prefs"] == {
        "gb_eye": "environment",
        "gb_vol": "0.4",
    }
    assert c.get("/api/growbot/personas").json()["personas"][0]["prefs"]["gb_vol"] == "0.4"
    url = f"/api/growbot/personas/{pid}/prefs"
    assert c.patch(url, json={"set": {"gb_vol": 1}}).status_code == 400
    assert c.patch(url, json={"set": {"k": "x" * 20_000}}).status_code == 413
    four = {f"k{i}": "x" * 15_000 for i in range(5)}
    assert c.patch(url, json={"set": four}).status_code == 413
    nobody = "/api/growbot/personas/00000000-0000-4000-8000-000000000000/prefs"
    assert c.patch(nobody, json={"set": {}}).status_code == 404


async def test_the_sidecars_settings_routes(maker: async_sessionmaker) -> None:
    c = _client(maker)
    pid = _make(c)["id"]
    repo = SqlPersonaRepo(maker)
    await SqlGrowbotRepo(maker).put_robot(OWNER, "gb-fish", "Fish")
    assert await repo.open_on_phone(SYSTEM_OWNER, pid, PHONE_A, "Pixel", "gb-fish")
    who = {"persona": pid, "phone": PHONE_A}
    assert c.post("/internal/growbot/prefs/pull", json=who).json() == {
        "version": 0,
        "prefs": None,
        "adopt": False,
    }
    r = c.post("/internal/growbot/prefs/push", json={**who, "set": {"gb_vol": "0.5"}})
    assert r.status_code == 200 and r.json() == {"version": 1}
    assert c.post("/internal/growbot/soul/state", json=who).json()["prefs"] == 1
    assert c.post("/internal/growbot/prefs/pull", json=who).json()["prefs"] == {"gb_vol": "0.5"}
    assert c.post("/internal/growbot/prefs/push", json={**who, "set": []}).status_code == 400
    four = {f"k{i}": "x" * 15_000 for i in range(5)}
    assert c.post("/internal/growbot/prefs/push", json={**who, "set": four}).status_code == 413
    other = {"persona": pid, "phone": PHONE_B}
    assert c.post("/internal/growbot/prefs/pull", json=other).status_code == 409
    assert c.post("/internal/growbot/prefs/push", json=other).status_code == 409
    assert c.post("/internal/growbot/prefs/pull", json={**who, "phone": "x"}).status_code == 401
    assert c.post("/internal/growbot/prefs/push", json={**who, "phone": "x"}).status_code == 401
    c.delete(f"/api/growbot/personas/{pid}")
    assert c.post("/internal/growbot/prefs/pull", json=who).status_code == 404


async def test_an_owners_settings_before_any_phone_leave_room_for_the_phones_own(
    maker: async_sessionmaker,
) -> None:
    repo, sprout, _ = await _world(maker)
    assert await repo.patch_prefs(OWNER, sprout, prefs.patch_from({"gb_eye": "environment"}, []))
    assert await repo.patch_prefs(OWNER, sprout, prefs.patch_from({"gb_vol": "0.4"}, []))
    # The phone is told the owner's, and that it should keep its own under them.
    assert await repo.phone_prefs(SYSTEM_OWNER, sprout, PHONE_A) == (
        2,
        {"gb_eye": "environment", "gb_vol": "0.4"},
        True,
    )
    p = await repo.get_persona(OWNER, sprout)
    assert p is not None and p.prefs == {"gb_eye": "environment", "gb_vol": "0.4"}
    # Once a phone sent its own, the marker is gone.
    mine = prefs.patch_from({"gb_face_color": "#ffffff"}, [])
    assert await repo.phone_patch_prefs(SYSTEM_OWNER, sprout, PHONE_A, mine) == 3
    got = await repo.phone_prefs(SYSTEM_OWNER, sprout, PHONE_A)
    assert got == (
        3,
        {"gb_eye": "environment", "gb_vol": "0.4", "gb_face_color": "#ffffff"},
        False,
    )
