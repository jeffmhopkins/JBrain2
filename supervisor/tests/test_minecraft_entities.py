"""Villagers, pets, named mobs and portals from the world's files (plan §M4e).

The records are real: `fixtures/mc_entities.json.gz` holds twelve actor records, the
`digp` entries naming them and the `portals` record, byte for byte, as BDS 1.26.52.3
wrote them in the design's experiment world (summoned librarians, a cleric, a nitwit,
a named cartographer, a tamed wolf "Rex", cat, parrot and horse "Clover", a pig named
"Bacon", a cow and a wild cat, and a portal lit by the console). The few facts the
experiment couldn't produce (an owner, a higher level, Mending) are patched into
those bytes in place, so the reader is always fed what the game writes.
"""

from __future__ import annotations

import base64
import gzip
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from tests.test_minecraft_mapping import _load, write_table
from tests.test_minecraft_sidecar import _make_world, _world_rig, bds, server

entities = _load("entities")
containers = _load("containers")

FIXTURE = Path(__file__).parent / "fixtures" / "mc_entities.json.gz"
RAW: dict[str, Any] = json.loads(gzip.decompress(FIXTURE.read_bytes()))
# A player's script id, the number `OwnerNew` holds for their pets.
HANK = -4294967295


def actor(label: str) -> tuple[bytes, bytes]:
    a = RAW["actors"][label]
    return bytes.fromhex(a["id"]), base64.b64decode(a["value"])


def patch(value: bytes, tag: int, name: str, fmt: str, new: int) -> bytes:
    """`value` with one named numeric tag's payload replaced, in place."""
    head = bytes([tag]) + struct.pack("<H", len(name)) + name.encode()
    at = value.index(head) + len(head)
    return value[:at] + struct.pack(fmt, new) + value[at + struct.calcsize(fmt) :]


def digp_records() -> list[tuple[bytes, bytes]]:
    return [(bytes.fromhex(d["key"]), bytes.fromhex(d["value"])) for d in RAW["digp"]]


def world(
    tmp: Path,
    changed: dict[str, bytes] | None = None,
    extra: list[tuple[bytes, bytes]] | None = None,
    digp: list[tuple[bytes, bytes]] | None = None,
) -> Path:
    """The fixture as a database: every actor (some patched), the digp records, the
    portals record."""
    records: list[tuple[bytes, int, bytes | None]] = []
    for label in RAW["actors"]:
        uid, value = actor(label)
        records.append((b"actorprefix" + uid, 1, (changed or {}).get(label, value)))
    records += [(k, 1, v) for k, v in (digp_records() if digp is None else digp)]
    records.append((b"portals", 1, base64.b64decode(RAW["portals"])))
    records += [(k, 1, v) for k, v in extra or []]
    db = tmp / "db"
    db.mkdir(parents=True, exist_ok=True)
    write_table(db / "000005.ldb", records)
    return db


def index_of(db: Path) -> Any:
    index = entities.EntityIndex(db)
    index.refresh()
    return index


# --- reading the records ---


def test_only_candidates_are_kept_and_each_is_placed_by_its_digp(
    tmp_path: Path,
) -> None:
    index = index_of(world(tmp_path))
    kinds = sorted((m.kind, m.name) for m in index.mobs)
    # The cow and the wild cat are dropped by the byte test, never parsed.
    assert kinds == [
        ("cat", ""),
        ("horse", "Clover"),
        ("parrot", ""),
        ("pig", "Bacon"),
        ("villager_v2", ""),
        ("villager_v2", ""),
        ("villager_v2", ""),
        ("villager_v2", ""),
        ("villager_v2", "Libby"),
        ("wolf", "Rex"),
    ]
    assert index.counts() == {
        "villagers_indexed": 5,
        "pets_indexed": 4,
        "named_indexed": 4,
        "records": 10,
        "unreadable_records": 0,
        "unplaced_records": 0,
        "overflow": {"villagers": 0, "pets": 0, "named": 0},
    }
    rex = next(m for m in index.mobs if m.name == "Rex")
    assert (rex.dim, rex.x, rex.y, rex.z) == (0, -344, 71, 164)
    assert rex.tamed and rex.owner is None  # summoned tame: nobody's (-1)
    assert rex.uid == -8589934591  # the script's `entity.id`, the probe showed


def test_the_byte_test_passes_exactly_the_candidates() -> None:
    for label in RAW["actors"]:
        _, value = actor(label)
        assert entities.is_candidate(value) == (label not in ("cow", "cat_wild")), label


def test_the_dimension_comes_from_the_digp_key(tmp_path: Path) -> None:
    """The actor's own NBT has no dimension: a 16-byte digp key carries it."""
    rex_id, _ = actor("rex")
    moved = []
    for key, value in digp_records():
        if rex_id in [value[i : i + 8] for i in range(0, len(value), 8)]:
            key = key[:12] + struct.pack("<i", 1)
        moved.append((key, value))
    index = index_of(world(tmp_path, digp=moved))
    rex = next(m for m in index.mobs if m.name == "Rex")
    assert rex.dim == 1
    assert index.pets(0, -300, 140, name="rex")[1] == 0
    found, n = index.pets(1, -300, 140, name="rex")
    assert n == 1 and found[0]["dim"] == 1


def test_an_actor_no_digp_lists_is_counted_not_placed(tmp_path: Path) -> None:
    rex_id, _ = actor("rex")
    kept = [
        (
            k,
            b"".join(
                v[i : i + 8] for i in range(0, len(v), 8) if v[i : i + 8] != rex_id
            ),
        )
        for k, v in digp_records()
    ]
    index = index_of(world(tmp_path, digp=kept))
    assert index.unplaced == 1 and not any(m.name == "Rex" for m in index.mobs)


def test_an_old_worlds_per_chunk_entity_record_is_read_too(tmp_path: Path) -> None:
    """Before 1.18.30 a chunk's entities were one tag-50 record of back-to-back
    compounds, placed by its own key."""
    _, rex = actor("rex")
    _, bacon = actor("bacon")
    legacy = struct.pack("<iii", -22, 10, 1) + bytes([50])
    index = index_of(world(tmp_path, extra=[(legacy, rex + bacon)]))
    in_nether = sorted(m.name for m in index.mobs if m.dim == 1)
    assert in_nether == ["Bacon", "Rex"]


def test_an_unreadable_candidate_is_skipped_and_counted(tmp_path: Path) -> None:
    _, rex = actor("rex")
    index = index_of(world(tmp_path, changed={"rex": rex[:-20]}))
    assert index.unreadable == 1 and len(index.mobs) == 9


def test_caps_keep_the_index_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entities, "MAX_PETS", 2)
    monkeypatch.setattr(entities, "MAX_VILLAGERS", 3)
    index = index_of(world(tmp_path))
    assert index.overflow == {"villagers": 2, "pets": 2, "named": 0}
    # A named pet over the pet cap is still kept as a named mob.
    assert sum(m.villager for m in index.mobs) == 3
    assert {"Rex", "Clover", "Bacon", "Libby"} <= {m.name for m in index.mobs}


def test_the_container_and_entity_indexes_share_one_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world(tmp_path)
    sweeps = []
    real = containers.leveldb.entries

    def counted(path: Path, wanted: Any = None) -> Any:
        sweeps.append(path)
        return real(path, wanted)

    monkeypatch.setattr(containers.leveldb, "entries", counted)
    chests, mobs = containers.ContainerIndex(db), entities.EntityIndex(db)
    containers.refresh_together([chests, mobs])
    assert len(sweeps) == 1 and len(mobs.mobs) == 10
    containers.refresh_together([chests, mobs], force=True)
    assert len(sweeps) == 1  # the files didn't change


# --- villagers and trades ---


def test_a_villager_tells_only_the_trades_it_offers_now(tmp_path: Path) -> None:
    """Owner, 2026-10-13: unlocked levels only. The files hold every level's trades,
    written before anyone opened the trade screen; a Novice shows its Novice ones."""
    index = index_of(world(tmp_path))
    found, n = index.villagers(0, -335, 111, profession="librarian", limit=1)
    lib = found[0]
    assert (lib["x"], lib["z"], lib["level_name"]) == (-335, 110, "Novice")
    assert all(t["tier"] == 0 for t in lib["trades"]) and lib["locked_trades"] > 0
    books = [t for t in lib["trades"] if t["sell"][0] == "minecraft:enchanted_book"]
    # Its Novice book is Piercing II for 29 emeralds and a book; its Apprentice one
    # (Riptide) is locked, so it isn't here, and nothing asks for it finds it.
    assert books == [
        {
            "tier": 0,
            "buy": ["minecraft:emerald", 29],
            "buy2": ["minecraft:book", 1],
            "sell": ["minecraft:enchanted_book", 1],
            "enchantments": [["piercing", 2]],
            "uses": 0,
            "max_uses": 12,
            "used_up": False,
        }
    ]
    assert index.villagers(0, 0, 0, enchant="riptide") == ([], 0)
    assert not lib["locked_in"] and n == 2


def test_a_higher_level_unlocks_its_trades(tmp_path: Path) -> None:
    _, lib = actor("librarian_a")
    index = index_of(
        world(tmp_path, {"librarian_a": patch(lib, 3, "TradeTier", "<i", 1)})
    )
    found, _ = index.villagers(0, 0, 0, enchant="riptide")
    assert found[0]["level_name"] == "Apprentice"
    assert found[0]["trades"][0]["enchantments"] == [["riptide", 1]]
    assert found[0]["trades"][0]["buy"] == ["minecraft:emerald", 7]


def test_mending_is_enchantment_26(tmp_path: Path) -> None:
    """Bedrock's numeric ids: the experiment's librarian sold Frost Walker (25) at
    Expert; the same record with 26 is a Mending book, at that level only."""
    _, lib = actor("librarian_b")
    frost = b"\x02\x02\x00id" + struct.pack("<h", 25)
    mending = lib.replace(frost, b"\x02\x02\x00id" + struct.pack("<h", 26))
    assert mending != lib and entities.ENCHANTMENTS[26] == "mending"
    novice = index_of(world(tmp_path / "a", {"librarian_b": mending}))
    assert novice.villagers(0, 0, 0, enchant="mending") == ([], 0)
    expert = patch(mending, 3, "TradeTier", "<i", 3)
    index = index_of(world(tmp_path / "b", {"librarian_b": expert}))
    found, n = index.villagers(
        0, -300, 140, enchant="mending", sells="minecraft:enchanted_book"
    )
    assert n == 1 and found[0]["level_name"] == "Expert"
    (trade,) = found[0]["trades"]
    assert trade["enchantments"] == [["mending", 1]] and trade["buy"] == [
        "minecraft:emerald",
        12,
    ]


def test_a_used_up_trade_says_so_and_a_traded_villager_is_locked_in(
    tmp_path: Path,
) -> None:
    _, cleric = actor("cleric")
    cleric = patch(cleric, 3, "TradeExperience", "<i", 10)
    cleric = patch(cleric, 3, "uses", "<i", 16)  # its first trade, 16 of 16
    index = index_of(world(tmp_path, {"cleric": cleric}))
    found, _ = index.villagers(0, 0, 0, profession="cleric")
    assert found[0]["locked_in"]
    assert found[0]["trades"][0]["used_up"] and found[0]["trades"][0]["uses"] == 16


def test_jobs_and_the_radius_and_limit(tmp_path: Path) -> None:
    index = index_of(world(tmp_path))
    jobs = sorted(h["job"] for h in index.villagers(0, -300, 140, limit=20)[0])
    assert jobs == ["cartographer", "cleric", "librarian", "librarian", "nitwit"]
    named = index.villagers(0, -300, 140, profession="cartographer")[0]
    assert (
        named[0]["name"] == "Libby"
    )  # summoned as a librarian; took a cartography table
    near, n = index.villagers(0, -335, 111, radius=20, limit=20)
    assert n == 2 and [h["distance"] for h in near] == sorted(
        h["distance"] for h in near
    )
    assert len(index.villagers(0, -300, 140, limit=2)[0]) == 2


# --- pets and named mobs ---


def test_a_players_pets_by_their_owner_id(tmp_path: Path) -> None:
    _, rex = actor("rex")
    _, clover = actor("clover")
    index = index_of(
        world(
            tmp_path,
            {
                "rex": patch(
                    patch(rex, 4, "OwnerNew", "<q", HANK), 1, "Sitting", "<b", 1
                ),
                "clover": patch(clover, 4, "OwnerNew", "<q", HANK),
            },
        )
    )
    mine, n = index.pets(0, -300, 140, owner=HANK)
    assert n == 2 and [p["name"] for p in mine] == ["Clover", "Rex"]
    rex_hit = mine[1]
    assert rex_hit["owner"] == str(HANK) and rex_hit["sitting"]
    assert rex_hit["distance"] == 50
    # Without an owner: every tamed pet in the world (owner, 2026-10-13: anyone's).
    assert index.pets(0, -300, 140)[1] == 4
    dogs = index.pets(0, -300, 140, kind="wolf")[0]
    assert [d["name"] for d in dogs] == ["Rex"]
    # A name finds any mob called that, the owner's first.
    assert index.pets(0, 0, 0, name="BACON")[0][0]["kind"] == "pig"
    assert [p["name"] for p in index.pets(0, 0, 0, name="e", owner=HANK)[0]][:2] == [
        "Clover",
        "Rex",
    ]
    assert index.pets(0, 0, 0, name="Rex")[1] == 1


def test_pets_in_other_dimensions_come_last_with_no_distance(tmp_path: Path) -> None:
    rex_id, _ = actor("rex")
    moved = [
        (k[:12] + struct.pack("<i", 1), v)
        if rex_id in [v[i : i + 8] for i in range(0, len(v), 8)]
        else (k, v)
        for k, v in digp_records()
    ]
    index = index_of(world(tmp_path, digp=moved))
    here, _ = index.pets(0, -300, 140, everywhere=True, limit=10)
    assert here[-1]["name"] == "Rex" and here[-1]["distance"] is None
    assert "Rex" not in [p["name"] for p in index.pets(0, -300, 140, limit=10)[0]]


def test_coats_are_named_in_words(tmp_path: Path) -> None:
    index = index_of(world(tmp_path))
    parrot = index.pets(0, 0, 0, kind="parrot")[0][0]
    assert parrot["coat"] == ["red"]


# --- portals ---


def test_the_portal_records(tmp_path: Path) -> None:
    db = world(tmp_path)
    assert entities.portals(db) == [
        {"dim": 0, "x": -280, "y": 65, "z": 151, "span": 2, "xa": 0, "za": 1}
    ]
    empty = tmp_path / "empty"
    empty.mkdir()
    write_table(empty / "000005.ldb", [(b"other", 1, b"x")])
    assert entities.portals(empty) == []


# --- the wrapper's routes ---


def _rig_on_fixture() -> Any:
    w = _make_world("world")
    db = w / "db"
    db.rmdir()
    world(w)
    return _world_rig()


def test_the_villager_and_pet_routes(tmp_path: Path) -> None:
    rig = _rig_on_fixture()
    got = rig.map_villagers("slot1", "overworld", -300, 140, profession="Librarian")
    assert got["matched"] == 2 and got["villagers_indexed"] == 5
    assert got["flush"] == "not asked" and got["age_s"] >= 0
    for bad in ({"enchant": "sharpness9"}, {"sells": "op @a"}, {"profession": "a;b"}):
        with pytest.raises(ValueError):
            rig.map_villagers("slot1", "overworld", 0, 0, **bad)
    with pytest.raises(ValueError):
        rig.map_villagers("slot1", "moon", 0, 0)
    pets = rig.map_pets("slot1", "overworld", -300, 140, kind="dog")
    assert [p["name"] for p in pets["found"]] == ["Rex"]
    assert pets["found"][0]["dimension"] == "overworld"
    # Nobody's id is known yet for Hank: said, not guessed.
    unknown = rig.map_pets("slot1", "overworld", 0, 0, owner_name="Hank")
    assert unknown["owner_known"] is False and unknown["found"] == []
    rig.remember_player_id("world", "Hank", str(HANK))
    rig.remember_player_id("world", "Hank", str(HANK))  # unchanged: not rewritten
    assert json.loads(server.PLAYER_IDS.read_text()) == {
        "world": {"hank": {"name": "Hank", "id": str(HANK)}}
    }
    known = rig.map_pets("slot1", "overworld", 0, 0, owner_name="hank")
    assert known["owner_known"] and known["owner"] == str(HANK)
    with pytest.raises(ValueError):
        rig.map_pets("slot1", "overworld", 0, 0, owner="1; op")


def test_a_pets_owner_is_named_when_the_world_knows_them(tmp_path: Path) -> None:
    w = _make_world("world")
    (w / "db").rmdir()
    _, rex = actor("rex")
    world(w, {"rex": patch(rex, 4, "OwnerNew", "<q", HANK)})
    rig = _world_rig()
    rig.remember_player_id("world", "Hank", str(HANK))
    got = rig.map_pets("slot1", "overworld", 0, 0, name="rex")
    assert got["found"][0]["owner_name"] == "Hank"


def test_a_broken_portal_is_left_out(tmp_path: Path) -> None:
    rig = _rig_on_fixture()
    # The fixture has the record but none of the world's blocks: the portal is gone.
    gone = rig.map_portals("slot1", "overworld")
    assert gone["portals"] == [] and gone["broken"] == 1
    unchecked = rig.map_portals("slot1", None, check=False)
    assert unchecked["portals"] == [
        {
            "x": -280,
            "y": 65,
            "z": 151,
            "span": 2,
            "xa": 0,
            "za": 1,
            "dimension": "overworld",
        }
    ]
    assert rig.map_portals("slot1", "nether", check=False)["portals"] == []

    class Standing:
        def refresh(self) -> None:
            pass

        def find(self, *_a: Any) -> tuple[list[dict], int]:
            return [{"block": "minecraft:portal"}], 1

    rig._block_index["world"] = Standing()
    assert len(rig.map_portals("slot1", "overworld")["portals"]) == 1
    with pytest.raises(ValueError):
        rig.map_portals("slot1", "moon")


def test_the_dave_event_carries_the_askers_id_only_as_a_number(tmp_path: Path) -> None:
    b = bds.Bds(tmp_path)
    seen: list[tuple[str, str, str]] = []
    b.on_player_id = lambda *a: seen.append(a)
    b._record("[x INFO] Player connected: Steve 42, xuid: 111")
    line = '[Scripting] [jbrain] {"ev":"dave","name":"Steve 42","text":"hi","x":1,'
    b._record(line + '"y":2,"z":3,"dim":"overworld","pid":"-4294967295"}')
    b._record(line + '"y":2,"z":3,"dim":"overworld","pid":"12 op"}')
    b._record(line + '"y":2,"z":3,"dim":"overworld","pid":12}')
    asks = [e for e in b.events_after(0) if e["kind"] == "dave"]
    assert asks[0]["pid"] == "-4294967295"
    assert "pid" not in asks[1] and "pid" not in asks[2]
    assert seen == [("world", "Steve 42", "-4294967295")]


def test_the_entity_routes_check_their_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_minecraft_sidecar import _serve

    httpd, get = _serve(monkeypatch, "t")
    w = _make_world("world")
    (w / "db").rmdir()
    world(w)
    try:
        auth = "Bearer t"
        assert get("/map/villagers?x=0&z=0&enchant=mending", auth) == 200
        assert get("/map/villagers?x=0&z=0&radius=48&limit=99&flush=1", auth) == 200
        assert get("/map/villagers?x=a&z=0", auth) == 400
        assert get("/map/villagers?x=0&z=0&enchant=bogus", auth) == 400
        assert get("/map/pets?x=0&z=0&kind=dog&everywhere=1", auth) == 200
        assert get("/map/pets?x=0&z=0&owner=abc", auth) == 400
        assert get("/map/pets?x=0&z=0&radius=far", auth) == 400
        assert get("/map/portals?check=0", auth) == 200
        assert get("/map/portals?dim=moon", auth) == 400
        assert get("/map/portals", None) == 401
    finally:
        httpd.shutdown()


# --- on the map: the listing (§M4e "on the map", owner 2026-10-13) ---


def _mob(uid: int, x: int, z: int, **kw: Any) -> Any:
    kind = kw.pop("kind", "villager_v2")
    return entities.Mob(uid, kind, kw.pop("dim", 0), x, 64, z, **kw)


def test_villagers_within_48_blocks_of_each_other_are_one_village() -> None:
    chain = [_mob(i + 1, i * 40, 0, job="farmer") for i in range(4)]  # 40 apart
    lone = _mob(9, 1000, 1000, job="librarian")
    got = entities.listing([*chain, lone], 0)
    assert [(v["count"], v["jobs"]) for v in got["villages"]] == [
        (4, {"farmer": 4}),
        (1, {"librarian": 1}),
    ]
    first = got["villages"][0]
    # Single link: the ends are 120 apart, joined through the middle.
    assert (first["x"], first["z"]) == (60, 0)
    assert {v["village"] for v in got["villagers"][:4]} == {first["id"]}


def test_the_grouping_stays_linear_on_a_big_world() -> None:
    crowd = [
        _mob(i + 1, (i % 100) * 3, (i // 100) * 3, job="mason") for i in range(5000)
    ]
    got = entities.listing(crowd, 0)
    assert len(got["villages"]) == 1 and got["villages"][0]["count"] == 5000


def test_traders_are_counted_not_grouped_and_a_named_pet_is_listed_once() -> None:
    mobs = [
        _mob(1, 0, 0, job="cleric"),
        _mob(2, 5, 5, kind="wandering_trader", job="wandering trader"),
        _mob(3, 10, 0, kind="wolf", name="Rex", tamed=True, owner=7, sitting=True),
        _mob(4, 20, 0, kind="cow", name="Clover"),
        _mob(5, 30, 0, name="Libby", job="cartographer"),
        _mob(6, 0, 0, kind="cat", tamed=True, dim=1),
    ]
    got = entities.listing(mobs, 0)
    assert got["traders"] == 1
    assert [v["count"] for v in got["villages"]] == [2]
    assert [p["name"] for p in got["pets"]] == ["Rex"]
    assert got["pets"][0]["owner"] == "7" and got["pets"][0]["sitting"]
    assert [m["name"] for m in got["named"]] == ["Clover"]
    assert entities.listing(mobs, 1)["pets"][0]["kind"] == "cat"


def test_a_box_narrows_what_is_listed_but_a_village_counts_all_its_villagers() -> None:
    mobs = [_mob(i + 1, i * 10, 0, job="farmer") for i in range(6)]  # x 0..50
    got = entities.listing(mobs, 0, (0, -10, 20, 10))
    assert [v["x"] for v in got["villagers"]] == [0, 10, 20]
    assert got["villages"] == []  # its centre, x 25, is outside the box
    assert entities.listing(mobs, 0, (20, -10, 30, 10))["villages"][0]["count"] == 6


def test_a_villager_with_no_unique_id_is_keyed_by_where_it_stands() -> None:
    m = _mob(0, -3, 7, job="nitwit")
    assert m.key == "at:0:-3:64:7"
    assert entities.villager_by_id([m], "at:0:-3:64:7")["job"] == "nitwit"
    assert entities.villager_by_id([m], "12") is None


def test_the_listing_caps_the_villagers_it_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entities, "MAX_LISTED_VILLAGERS", 3)
    got = entities.listing([_mob(i + 1, i, 0, job="farmer") for i in range(5)], 0)
    assert len(got["villagers"]) == 3 and got["villagers_truncated"]
    assert got["villages"][0]["count"] == 5


def test_the_entities_route_and_a_villagers_trades_by_id(tmp_path: Path) -> None:
    rig = _rig_on_fixture()
    got = rig.map_entities("slot1", "overworld")
    assert sum(v["count"] for v in got["villages"]) == 5
    assert {p["name"] for p in got["pets"]} >= {"Rex"}
    assert {m["name"] for m in got["named"]} == {"Bacon"}
    assert got["live"] is False and got["age_s"] >= 0 and "flush" not in got
    lib = next(v for v in got["villagers"] if v["job"] == "librarian")
    card = rig.map_villager("slot1", lib["id"])["found"]
    assert card["dimension"] == "overworld" and card["trades"]
    assert all(t["tier"] <= card["level"] for t in card["trades"])
    assert rig.map_villager("slot1", "123")["found"] is None
    with pytest.raises(ValueError):
        rig.map_villager("slot1", "1; op")
    with pytest.raises(ValueError):
        rig.map_entities("slot1", "moon")
    with pytest.raises(ValueError):
        rig.map_entities("slot1", "overworld", (10, 0, 0, 0))


def test_the_entities_routes_check_their_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_minecraft_sidecar import _serve

    httpd, get = _serve(monkeypatch, "t")
    w = _make_world("world")
    (w / "db").rmdir()
    world(w)
    try:
        auth = "Bearer t"
        assert get("/map/entities?dim=overworld", auth) == 200
        assert get("/map/entities?x0=-400&z0=0&x1=0&z1=200", auth) == 200
        assert get("/map/entities?x0=-400&z0=0", auth) == 400
        assert get("/map/entities?x0=a&z0=0&x1=1&z1=1", auth) == 400
        assert get("/map/entities?dim=moon", auth) == 400
        assert get("/map/villagers?id=-8589934591", auth) == 200
        assert get("/map/villagers?id=bogus", auth) == 400
        assert get("/map/entities", None) == 401
    finally:
        httpd.shutdown()
