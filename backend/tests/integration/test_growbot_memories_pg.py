"""Migration 0258 against real Postgres: a persona's visual memories are owner-only, sync only
with the phone that holds the persona, go into checkpoints and come back with a restore, and
their pictures are deleted from their namespace only once nothing names them.

The mandatory RLS isolation test for `growbot_memories` (CLAUDE.md rule 3), then the rules of
plan §2c wave G4d, over the real repo and then end to end over the routes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
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
from jbrain.growbot import memories, personas, souls
from jbrain.growbot.memories import Saved
from jbrain.growbot.personas import SqlPersonaRepo
from jbrain.growbot.repo import SqlGrowbotRepo
from jbrain.storage import FsBlobStore
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
PHONE_A = "phone-aaaaaaaaaaaaaaaa"
PHONE_B = "phone-bbbbbbbbbbbbbbbb"
FACE = b"\xff\xd8\xff\xe0face-one"
CUP = b"\xff\xd8\xff\xe0blue-cup"
SIDE = b"\xff\xd8\xff\xe0face-side"


def _meta(name: str = "my person", kind: str = "person", t: int = 1_762_000_000_000) -> dict:
    return {
        "kind": kind,
        "name": name,
        "desc": "short hair, round glasses",
        "t": t,
        "confirmed": "human",
        "namedAt": t,
    }


def _saved(mid: str, *pics: bytes, **meta: Any) -> Saved:
    return Saved(
        id=mid, meta=_meta(**meta), views=[(p, 1_762_000_000_000 + i) for i, p in enumerate(pics)]
    )


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    made = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(made, SYSTEM_OWNER) as session:
        for table in (
            "growbot_tickets",
            "growbot_memories",
            "growbot_body",
            "growbot_checkpoints",
            "growbot_personas",
            "growbot_phones",
            "growbot_robots",
        ):
            await session.execute(text(f"DELETE FROM app.{table}"))
    yield made
    await engine.dispose()


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path / "blobs").namespace("growbot-memories")


async def _world(maker: async_sessionmaker, blobs: FsBlobStore) -> tuple[SqlPersonaRepo, str, str]:
    """Sprout open on phone A with one saved face; Bolt not open."""
    repo = SqlPersonaRepo(maker, blobs)
    sprout = (await repo.create(OWNER, "Sprout", souls.fresh("Sprout"), "a fresh creature")).id
    bolt = (await repo.create(OWNER, "Bolt", souls.fresh("Bolt"), "a fresh creature")).id
    assert await repo.open_on_phone(SYSTEM_OWNER, sprout, PHONE_A, "Pixel 6a", "")
    assert await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r1", FACE)) == 1
    return repo, sprout, bolt


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- isolation ------------------------------------------------------------------------------


async def test_memories_are_owner_only(maker: async_sessionmaker, blobs: FsBlobStore) -> None:
    repo, sprout, _ = await _world(maker, blobs)
    async with scoped_session(maker, OWNER) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM app.growbot_memories"))
        ).scalar_one() == 1
    for ctx in NON_OWNERS:
        async with scoped_session(maker, ctx) as session:
            assert (
                await session.execute(text("SELECT count(*) FROM app.growbot_memories"))
            ).scalar_one() == 0, ctx


@pytest.mark.parametrize("ctx", NON_OWNERS)
async def test_a_non_owner_can_neither_read_nor_change_memories(
    maker: async_sessionmaker, blobs: FsBlobStore, ctx: SessionContext
) -> None:
    repo, sprout, _ = await _world(maker, blobs)
    assert await repo.memories(ctx, sprout) is None
    assert await repo.memory_view(ctx, sprout, "r1", 0) is None
    assert not await repo.forget_memory(ctx, sprout, "r1")
    assert await repo.phone_memories(ctx, sprout, PHONE_A) == "gone"
    assert await repo.phone_view(ctx, sprout, PHONE_A, "r1", 0) == "gone"
    assert await repo.phone_keep(ctx, sprout, PHONE_A, _saved("r2", CUP)) == "gone"
    assert await repo.phone_forget(ctx, sprout, PHONE_A, ["r1"]) == "gone"
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.growbot_memories (persona_id, id, meta, views)"
                    " VALUES (:p, 'r9', '{}', CAST(:v AS jsonb))"
                ),
                {"p": sprout, "v": '[{"sha": "x", "t": 1}]'},
            )
    # Nothing moved, and the picture is still there.
    got = await repo.memories(OWNER, sprout)
    assert got is not None and [m.id for m in got] == ["r1"]
    assert await blobs.exists(_sha(FACE))


# -- the phone ------------------------------------------------------------------------------


async def test_only_the_phone_holding_it_syncs_its_memories(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    repo, sprout, bolt = await _world(maker, blobs)
    held = await repo.phone_memories(SYSTEM_OWNER, sprout, PHONE_A)
    assert not isinstance(held, str)
    version, rows = held
    assert version == 1 and [(m.id, m.meta, [v["t"] for v in m.views]) for m in rows] == [
        ("r1", _meta(), [1_762_000_000_000])
    ]
    assert await repo.phone_view(SYSTEM_OWNER, sprout, PHONE_A, "r1", 0) == FACE
    assert await repo.phone_view(SYSTEM_OWNER, sprout, PHONE_A, "r1", 1) is None
    assert await repo.phone_view(SYSTEM_OWNER, sprout, PHONE_A, "r404", 0) is None
    # Another phone, or a persona this phone does not hold, gets nothing.
    assert await repo.phone_memories(SYSTEM_OWNER, sprout, PHONE_B) == "moved"
    assert await repo.phone_memories(SYSTEM_OWNER, bolt, PHONE_A) == "moved"
    assert await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_B, _saved("r2", CUP)) == "moved"
    assert await repo.phone_forget(SYSTEM_OWNER, sprout, PHONE_B, ["r1"]) == "moved"
    assert await repo.phone_view(SYSTEM_OWNER, sprout, PHONE_B, "r1", 0) == "moved"
    assert await repo.phone_memories(SYSTEM_OWNER, "nope", PHONE_A) == "gone"
    # A second view and a rename land whole; the picture it no longer names goes.
    assert (
        await repo.phone_keep(
            SYSTEM_OWNER, sprout, PHONE_A, _saved("r1", SIDE, FACE, name="Quorra")
        )
        == 2
    )
    got = await repo.memories(OWNER, sprout)
    assert got is not None and got[0].meta["name"] == "Quorra" and len(got[0].views) == 2
    assert (
        await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r1", SIDE, name="Quorra")) == 3
    )
    assert not await blobs.exists(_sha(FACE)) and await blobs.exists(_sha(SIDE))
    # Forgetting what is already gone changes nothing, and says the version as it is.
    assert await repo.phone_forget(SYSTEM_OWNER, sprout, PHONE_A, ["r404"]) == 3
    assert await repo.phone_forget(SYSTEM_OWNER, sprout, PHONE_A, ["r1"]) == 4
    assert await repo.memories(OWNER, sprout) == []
    assert not await blobs.exists(_sha(SIDE))
    # The poll sees every change.
    pulled = await repo.pull(SYSTEM_OWNER, sprout, PHONE_A, with_soul=False)
    assert not isinstance(pulled, str) and pulled.memories == 4


async def test_a_persona_keeps_at_most_max_memories(
    maker: async_sessionmaker, blobs: FsBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sprout, _ = await _world(maker, blobs)
    monkeypatch.setattr(personas, "MAX_MEMORIES", 2)
    assert await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r2", CUP)) == 2
    assert await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r3", SIDE)) == "full"
    # A full persona can still rename or re-view one it has.
    assert await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r2", CUP, name="mug")) == 3
    assert not await blobs.exists(_sha(SIDE))
    # Nor any new picture once the disk runs low.
    monkeypatch.setattr(personas, "MAX_MEMORIES", 50)
    monkeypatch.setattr(blobs, "free_bytes", lambda: personas.FREE_FLOOR_BYTES - 1)
    assert await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r4", SIDE)) == "full"
    assert not await blobs.exists(_sha(SIDE))


# -- checkpoints, restore, reset, duplicate, delete ------------------------------------------


async def test_a_checkpoint_keeps_the_memories_and_a_restore_brings_them_back(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    repo, sprout, _ = await _world(maker, blobs)
    cp = await repo.save_checkpoint(OWNER, sprout, "First week")
    assert cp is not None and cp.memories == 1
    # The owner deletes the face; the checkpoint still names its picture, so it stays.
    assert await repo.forget_memory(OWNER, sprout, "r1")
    assert not await repo.forget_memory(OWNER, sprout, "r1")
    assert await repo.memories(OWNER, sprout) == []
    assert await blobs.exists(_sha(FACE))
    assert (
        await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r2", CUP, kind="object")) == 3
    )
    done = await repo.write_soul(
        OWNER,
        sprout,
        souls.fresh("Sprout"),
        before=("Before restoring", "x"),
        memories=("from", cp.id),
    )
    assert done is not None and done != "stale"
    persona, before = done
    assert persona.memories == 1 and before is not None and before.memories == 1
    got = await repo.memories(OWNER, sprout)
    assert got is not None and [m.id for m in got] == ["r1"]
    assert await repo.memory_view(OWNER, sprout, "r1", 0) == FACE
    # The cup is kept by the checkpoint made before the restore, so it can be undone.
    assert await blobs.exists(_sha(CUP))
    pulled = await repo.pull(SYSTEM_OWNER, sprout, PHONE_A, with_soul=False)
    assert not isinstance(pulled, str) and pulled.memories == 4


async def test_a_reset_clears_them_and_its_checkpoint_keeps_them(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    repo, sprout, _ = await _world(maker, blobs)
    done = await repo.write_soul(
        OWNER, sprout, souls.fresh("Sprout"), before=("Before reset", "r"), memories="clear"
    )
    assert done is not None and done != "stale"
    assert done[0].memories == 0 and done[1] is not None and done[1].memories == 1
    assert await blobs.exists(_sha(FACE))
    # An edit keeps them.
    await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r2", CUP))
    edited = await repo.write_soul(
        OWNER, sprout, souls.fresh("Sprout"), before=("Before edit", "e")
    )
    assert edited is not None and edited != "stale" and edited[0].memories == 1


async def test_pruning_and_deleting_sweep_only_pictures_nothing_names(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    repo, sprout, bolt = await _world(maker, blobs)
    # A duplicate shares the face's picture.
    copy = await repo.create(
        OWNER, "Sprout copy", souls.fresh("Sprout copy"), "copy", memories_from=sprout
    )
    assert copy.memories == 1
    assert await repo.forget_memory(OWNER, sprout, "r1")
    assert await blobs.exists(_sha(FACE))
    # Checkpoints of the copy keep it too, past the original's delete.
    await repo.save_checkpoint(OWNER, copy.id, "keep")
    assert await repo.delete(OWNER, sprout)
    assert await repo.forget_memory(OWNER, copy.id, "r1")
    assert await blobs.exists(_sha(FACE))
    assert await repo.delete(OWNER, copy.id)
    assert not await blobs.exists(_sha(FACE))
    # Automatic checkpoints pruned past AUTO_KEEP let go of what only they named.
    assert await repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_A, "", "")
    await repo.phone_keep(SYSTEM_OWNER, bolt, PHONE_A, _saved("r5", CUP))
    await repo.save_checkpoint(OWNER, bolt, "auto", auto=True)
    await repo.phone_forget(SYSTEM_OWNER, bolt, PHONE_A, ["r5"])
    assert await blobs.exists(_sha(CUP))
    for _ in range(personas.AUTO_KEEP):
        await repo.save_checkpoint(OWNER, bolt, "auto", auto=True)
    assert not await blobs.exists(_sha(CUP))


async def test_racing_keeps_and_forgets_never_lose_a_named_picture(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    repo, sprout, bolt = await _world(maker, blobs)
    assert await repo.open_on_phone(SYSTEM_OWNER, bolt, PHONE_B, "", "")
    for _ in range(5):
        # Bolt saves the same picture Sprout forgets at the same moment.
        await asyncio.gather(
            repo.phone_keep(SYSTEM_OWNER, bolt, PHONE_B, _saved("r7", FACE)),
            repo.phone_forget(SYSTEM_OWNER, sprout, PHONE_A, ["r1"]),
        )
        assert await repo.phone_view(SYSTEM_OWNER, bolt, PHONE_B, "r7", 0) == FACE
        await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r1", FACE))
        await repo.phone_forget(SYSTEM_OWNER, bolt, PHONE_B, ["r7"])


async def test_the_schema_bounds_ids_and_views(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    _, sprout, _ = await _world(maker, blobs)
    for mid, views in (
        ("x1", '[{"sha":"a","t":1}]'),
        ("r2", "[]"),
        ("r3", "[1,2,3,4]"),
        ("r4", "{}"),
    ):
        with pytest.raises(DBAPIError):
            async with scoped_session(maker, OWNER) as session:
                await session.execute(
                    text(
                        "INSERT INTO app.growbot_memories (persona_id, id, meta, views)"
                        " VALUES (:p, :m, '{}', CAST(:v AS jsonb))"
                    ),
                    {"p": sprout, "m": mid, "v": views},
                )


# -- the routes, over the real repo ---------------------------------------------------------

OWNER_P = PrincipalInfo(id="00000000-0000-0000-0000-000000000001", kind="owner", label="owner")


def _client(maker: async_sessionmaker, blobs: FsBlobStore) -> TestClient:
    app = FastAPI()
    for module in (growbot, growbot_personas):
        app.include_router(module.router, prefix="/api")
        app.include_router(module.debug_router, prefix="/api")
        app.include_router(module.internal_router, prefix="/internal")
    app.state.settings = SimpleNamespace(lan_addr="https://jbrain.local")
    app.state.growbot_repo = SqlGrowbotRepo(maker)
    app.state.growbot_personas = SqlPersonaRepo(maker, blobs)
    app.dependency_overrides[owner_only] = lambda: OWNER_P
    app.dependency_overrides[current_debug_principal] = lambda: OWNER_P
    app.dependency_overrides[require_growbot] = lambda: None
    return TestClient(app)


def _record(mid: str = "r1a", name: str = "my person", *pics: bytes) -> dict[str, Any]:
    return {
        "id": mid,
        **_meta(name),
        "views": [
            {"j": base64.b64encode(p).decode(), "t": 1_762_000_000_000 + i}
            for i, p in enumerate(pics or (FACE,))
        ],
    }


async def test_routes_sync_list_view_forget_and_checkpoint(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    c = _client(maker, blobs)
    made = c.post("/api/growbot/personas", json={"name": "Sprout"}).json()
    pid = made["id"]
    assert made["memories"] == 0
    repo = SqlPersonaRepo(maker, blobs)
    assert await repo.open_on_phone(SYSTEM_OWNER, pid, PHONE_A, "", "")
    who = {"persona": pid, "phone": PHONE_A}

    kept = c.post("/internal/growbot/memories/keep", json={**who, "memory": _record()})
    assert kept.status_code == 200 and kept.json() == {"version": 1}
    c.post("/internal/growbot/memories/keep", json={**who, "memory": _record("r2b", "", CUP)})
    listed = c.post("/internal/growbot/memories/list", json=who).json()
    assert listed["version"] == 2
    assert {m["id"]: m for m in listed["memories"]}["r1a"] == {
        "id": "r1a",
        **_meta(),
        "views": [{"t": 1_762_000_000_000}],
    }
    view = c.post("/internal/growbot/memories/view", json={**who, "id": "r1a", "i": 0})
    assert view.status_code == 200 and view.content == FACE
    assert view.headers["content-type"] == "image/jpeg"
    assert (
        c.post("/internal/growbot/memories/view", json={**who, "id": "r1a", "i": 2}).status_code
        == 404
    )
    assert c.post("/internal/growbot/memories/view", json={**who, "id": "bad"}).status_code == 401
    state = c.post("/internal/growbot/soul/state", json=who).json()
    assert state["memories"] == 2

    # What the box will not keep is refused, so the phone stops sending it.
    for bad in (
        {**_record(), "views": [{"j": base64.b64encode(b"GIF89a").decode(), "t": 1}]},
        {**_record(), "views": [{"j": "not base64!", "t": 1}]},
        {**_record(), "kind": "pet"},
        {**_record(), "name": "x" * 61},
        {**_record(), "id": "R1"},
        {**_record(), "t": True},
        {**_record(), "views": []},
        {**_record(), "confirmed": "maybe"},
        "a string",
    ):
        r = c.post("/internal/growbot/memories/keep", json={**who, "memory": bad})
        assert r.status_code == 400, bad
    elsewhere = {"persona": pid, "phone": PHONE_B}
    assert c.post("/internal/growbot/memories/list", json=elsewhere).status_code == 409
    r = c.post("/internal/growbot/memories/keep", json={**elsewhere, "memory": _record("r3c")})
    assert r.status_code == 409 and r.json()["detail"] == "moved"
    assert (
        c.post("/internal/growbot/memories/list", json={"persona": pid, "phone": "x"}).status_code
        == 401
    )
    assert (
        c.post("/internal/growbot/memories/forget", json={**who, "ids": "r1a"}).status_code == 400
    )
    assert (
        c.post("/internal/growbot/memories/forget", json={**who, "ids": ["no"]}).status_code == 400
    )

    # The owner's page.
    mine = c.get(f"/api/growbot/personas/{pid}/memories").json()["memories"]
    assert [(m["id"], m["kind"], m["name"], m["views"], m["confirmed"]) for m in mine] == [
        ("r1a", "person", "my person", 1, "human"),
        ("r2b", "person", "", 1, "human"),
    ]
    assert mine[0]["saved_at"].startswith("2025-11-")
    pic = c.get(f"/api/growbot/personas/{pid}/memories/r1a/views/0")
    assert pic.status_code == 200 and pic.content == FACE
    assert pic.headers["cache-control"] == "private, no-store"
    assert c.get(f"/api/growbot/personas/{pid}/memories/r1a/views/3").status_code == 422
    assert c.get(f"/api/growbot/personas/{pid}/memories/r9/views/0").status_code == 404
    assert c.get(f"/api/growbot/personas/{pid}/memories/BAD/views/0").status_code == 422
    assert (
        c.get("/api/growbot/personas/00000000-0000-0000-0000-000000000000/memories").status_code
        == 404
    )
    assert c.get(f"/api/debug/growbot/personas/{pid}/memories").json() == {"memories": mine}
    assert c.get(f"/api/growbot/personas/{pid}").json()["memories"] == 2

    cp = c.post(f"/api/growbot/personas/{pid}/checkpoints", json={"name": "Both"}).json()
    assert cp["memories"] == 2
    assert c.delete(f"/api/growbot/personas/{pid}/memories/r2b").status_code == 204
    assert c.delete(f"/api/growbot/personas/{pid}/memories/r2b").status_code == 404
    assert c.post("/internal/growbot/soul/state", json=who).json()["memories"] == 3
    # The phone forgets one; the export never carries a picture.
    assert c.post("/internal/growbot/memories/forget", json={**who, "ids": ["r1a"]}).json() == {
        "version": 4
    }
    exported = c.get(f"/api/growbot/personas/{pid}/export")
    assert base64.b64encode(FACE).decode() not in exported.text and "r1a" not in exported.text

    # A restore brings both back; a reset clears them; Undo (restoring its checkpoint) undoes it.
    restored = c.post(f"/api/growbot/personas/{pid}/checkpoints/{cp['id']}/restore").json()
    assert restored["persona"]["memories"] == 2
    reset = c.post(f"/api/growbot/personas/{pid}/reset").json()
    assert reset["persona"]["memories"] == 0 and reset["checkpoint"]["memories"] == 2
    undo = c.post(f"/api/growbot/personas/{pid}/checkpoints/{reset['checkpoint']['id']}/restore")
    assert undo.json()["persona"]["memories"] == 2
    # A duplicate has them; an import over it clears them.
    dup = c.post(f"/api/growbot/personas/{pid}/duplicate").json()
    assert dup["memories"] == 2
    soul_file = c.get(f"/api/growbot/personas/{pid}/export").json()
    over = c.post("/api/growbot/personas/import", json={"soul": soul_file, "over": pid}).json()
    assert over["persona"]["memories"] == 0 and over["checkpoint"]["memories"] == 2
    assert memories.MAX_VIEWS == 3


async def test_a_checkpoint_from_before_memories_keeps_them_on_restore(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    repo, sprout, _ = await _world(maker, blobs)
    cp = await repo.save_checkpoint(OWNER, sprout, "Before G4d")
    assert cp is not None
    # As every checkpoint made before migration 0258 is: it never captured them.
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("UPDATE app.growbot_checkpoints SET memories = NULL WHERE id = :c"), {"c": cp.id}
        )
    listed = await repo.list_checkpoints(OWNER, sprout)
    assert [c.memories for c in listed] == [0]
    done = await repo.write_soul(
        OWNER,
        sprout,
        souls.fresh("Sprout"),
        before=("Before restoring", "x"),
        memories=("from", cp.id),
    )
    assert done is not None and done != "stale" and done[0].memories == 1
    got = await repo.memories(OWNER, sprout)
    assert got is not None and [m.id for m in got] == ["r1"]


async def test_checkpoints_racing_forgets_never_name_a_swept_picture(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    repo, sprout, _ = await _world(maker, blobs)
    for _ in range(5):
        cp, _gone = await asyncio.gather(
            repo.save_checkpoint(OWNER, sprout, "race"),
            repo.phone_forget(SYSTEM_OWNER, sprout, PHONE_A, ["r1"]),
        )
        assert cp is not None
        if cp.memories:
            assert await blobs.exists(_sha(FACE))
        await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r1", FACE))


async def test_an_owners_delete_racing_the_phones_writes_never_deadlocks(
    maker: async_sessionmaker, blobs: FsBlobStore
) -> None:
    repo, sprout, _ = await _world(maker, blobs)
    for _ in range(5):
        await asyncio.gather(
            repo.forget_memory(OWNER, sprout, "r1"),
            repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r1", FACE, name="renamed")),
            repo.phone_forget(SYSTEM_OWNER, sprout, PHONE_A, ["r1"]),
        )
        await repo.phone_keep(SYSTEM_OWNER, sprout, PHONE_A, _saved("r1", FACE))
