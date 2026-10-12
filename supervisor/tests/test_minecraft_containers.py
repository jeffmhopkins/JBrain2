"""Chest contents (plan §M23): the NBT reader and the container index, against a real
LevelDB written on disk the way the map's tests write one.

The block-entity shapes pinned here (`id`, `x`/`y`/`z`, `Items` of `Name`/`Count`, a
shulker box item's `tag.Items`, `pairx`/`pairz`, `LootTable`) are Bedrock's documented
format; the box check in §M23 confirms them against the owner's imported world.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any

import pytest

from tests.test_minecraft_mapping import _load, write_log, write_table

nbt = _load("nbt")
containers = _load("containers")


# --- a little-endian NBT writer, typed: (tag, value) ----------------------------------


def _str(s: str) -> bytes:
    raw = s.encode()
    return struct.pack("<H", len(raw)) + raw


def _payload(tag: int, value: Any) -> bytes:
    fixed = {1: "<b", 2: "<h", 3: "<i", 4: "<q", 5: "<f", 6: "<d"}
    if tag in fixed:
        return struct.pack(fixed[tag], value)
    if tag == 7:
        return struct.pack("<i", len(value)) + bytes(value)
    if tag == 8:
        return _str(value)
    if tag == 9:
        inner, items = value
        return struct.pack("<bi", inner, len(items)) + b"".join(
            _payload(inner, v) for v in items
        )
    if tag == 10:
        return (
            b"".join(
                bytes([t]) + _str(k) + _payload(t, v) for k, (t, v) in value.items()
            )
            + b"\0"
        )
    if tag in (11, 12):
        return struct.pack("<i", len(value)) + struct.pack(
            f"<{len(value)}{'i' if tag == 11 else 'q'}", *value
        )
    raise AssertionError(tag)


def root(body: dict[str, tuple[int, Any]], name: str = "") -> bytes:
    return b"\x0a" + _str(name) + _payload(10, body)


def S(v: str) -> tuple[int, str]:
    return (8, v)


def I(v: int) -> tuple[int, int]:  # noqa: E743 — the tag's own name
    return (3, v)


def item(name: str, count: int, **extra: tuple[int, Any]) -> dict[str, tuple[int, Any]]:
    return {
        "Name": S(name),
        "Count": (1, count),
        "Damage": (2, 0),
        "Slot": (1, 0),
        **extra,
    }


def block_entity(
    kind: str,
    x: int,
    y: int,
    z: int,
    items: list | None = None,
    **extra: tuple[int, Any],
) -> bytes:
    body: dict[str, tuple[int, Any]] = {"id": S(kind), "x": I(x), "y": I(y), "z": I(z)}
    if items is not None:
        body["Items"] = (9, (10, items))
    return root({**body, **extra})


def be_key(cx: int, cz: int, dim: int = 0) -> bytes:
    return (
        struct.pack("<ii", cx, cz) + (struct.pack("<i", dim) if dim else b"") + b"\x31"
    )


# --- NBT ------------------------------------------------------------------------------


def test_every_tag_reads_back_as_written() -> None:
    body = {
        "b": (1, -5),
        "s": (2, -300),
        "i": (3, 70000),
        "l": (4, -(1 << 40)),
        "f": (5, 1.5),
        "d": (6, -2.25),
        "ba": (7, b"\x01\x02\xff"),
        "str": S("héllo"),
        "list": (9, (3, [1, 2, 3])),
        "empty": (9, (0, [])),
        "nested": (10, {"deep": (9, (10, [{"k": S("v")}]))}),
        "ia": (11, [1, -1, 1 << 30]),
        "la": (12, [1 << 50, -7]),
    }
    name, value, end = nbt.read(root(body, "top"))
    assert name == "top" and end == len(root(body, "top"))
    assert value == {
        "b": -5,
        "s": -300,
        "i": 70000,
        "l": -(1 << 40),
        "f": 1.5,
        "d": -2.25,
        "ba": b"\x01\x02\xff",
        "str": "héllo",
        "list": [1, 2, 3],
        "empty": [],
        "nested": {"deep": [{"k": "v"}]},
        "ia": [1, -1, 1 << 30],
        "la": [1 << 50, -7],
    }


def test_back_to_back_compounds_are_each_read() -> None:
    record = root({"a": I(1)}) + root({"a": I(2)}) + root({"a": I(3)})
    assert [v["a"] for v in nbt.read_all(record)] == [1, 2, 3]
    assert nbt.read_all(b"") == []


def _nested(depth: int) -> bytes:
    body: dict[str, tuple[int, Any]] = {"leaf": I(1)}
    for _ in range(depth):
        body = {"c": (10, body)}
    return root(body)


@pytest.mark.parametrize(
    "bad",
    [
        root({"a": S("abc")})[:-3],  # truncated mid-string
        b"\x0a\x00\x00\x03\x01\x00a",  # an int with no bytes
        b"\x0a\x00\x00\x07\x01\x00a" + struct.pack("<i", -1) + b"\0",  # negative length
        b"\x0a\x00\x00\x09\x01\x00a\x0a"
        + struct.pack("<i", 1 << 30)
        + b"\0",  # huge list
        b"\x0a\x00\x00\x09\x01\x00a\x00"
        + struct.pack("<i", 5)
        + b"\0",  # END list, 5 long
        b"\x0a\x00\x00\x0d\x01\x00a\0",  # tag 13 doesn't exist
        b"\x00",  # no root
        b"\x0a\x00",  # a root with half a name
    ],
)
def test_a_malformed_record_is_refused_not_trusted(bad: bytes) -> None:
    with pytest.raises(nbt.NbtError):
        nbt.read_all(bad)


def test_depth_and_size_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    assert nbt.read(_nested(10))[1]["c"]["c"]
    with pytest.raises(nbt.NbtError, match="deep"):
        nbt.read(_nested(nbt.MAX_DEPTH + 1))
    with pytest.raises(nbt.NbtError, match="deep"):
        nbt.read(_nested(5), max_depth=3)
    monkeypatch.setattr(nbt, "MAX_BYTES", 16)
    with pytest.raises(nbt.NbtError, match="large"):
        nbt.read(root({"a": S("x" * 20)}))


# --- the container index --------------------------------------------------------------


def _world(tmp_path: Path, records: list[tuple[bytes, bytes | None]]) -> Path:
    write_log(tmp_path / "000010.log", 100, records)
    return tmp_path


def test_containers_are_read_with_what_they_hold(tmp_path: Path) -> None:
    shulker = item(
        "minecraft:red_shulker_box",
        1,
        tag=(10, {"Items": (9, (10, [item("minecraft:diamond", 30)]))}),
    )
    chunk = (
        block_entity(
            "Chest",
            5,
            64,
            5,
            [item("minecraft:diamond", 12), item("minecraft:torch", 64), shulker],
            CustomName=S("Loot"),
        )
        + block_entity("Furnace", 8, 64, 8, [item("minecraft:iron_ingot", 3)])
        + block_entity("EnderChest", 9, 64, 9, [item("minecraft:diamond", 64)])
        + block_entity("Sign", 2, 64, 2)
        + block_entity("ItemFrame", 3, 70, 3, Item=(10, item("minecraft:diamond", 1)))
        + block_entity(
            "Lectern", 4, 64, 4, book=(10, item("minecraft:writable_book", 1))
        )
        + block_entity("DecoratedPot", 6, 64, 6, item=(10, item("emerald", 2)))
    )
    index = containers.ContainerIndex(_world(tmp_path, [(be_key(0, 0), chunk)]))
    index.refresh()
    found, totals, matched = index.find(
        0, 0, 0, {"minecraft:diamond"}, limit=5, radius=None
    )
    assert matched == 2 and totals == {"minecraft:diamond": 43}
    # Nearest first; the shulker box's 30 count toward its chest and are flagged.
    assert [(h["kind"], h["items"]) for h in found] == [
        ("item frame", {"minecraft:diamond": 1}),
        ("chest", {"minecraft:diamond": 42}),
    ]
    chest = found[1]
    assert chest["name"] == "Loot" and chest["in_shulker_boxes"] == {
        "minecraft:diamond": 30
    }
    assert (chest["x"], chest["y"], chest["z"]) == (5, 64, 5)
    assert chest["distance"] == round(math.dist((0, 0), (5, 5)))
    held = index.names(0)
    assert {
        "minecraft:writable_book",
        "minecraft:emerald",
        "minecraft:iron_ingot",
    } <= held
    assert "minecraft:red_shulker_box" in held
    # The ender chest's 64 are a player's own, never read.
    assert totals["minecraft:diamond"] == 43


def test_a_double_chest_is_one_chest(tmp_path: Path) -> None:
    left = block_entity(
        "Chest",
        10,
        64,
        20,
        [item("minecraft:gold_ingot", 5)],
        pairx=I(11),
        pairz=I(20),
        pairlead=(1, 1),
    )
    right = block_entity(
        "Chest", 11, 64, 20, [item("minecraft:gold_ingot", 7)], pairx=I(10), pairz=I(20)
    )
    index = containers.ContainerIndex(_world(tmp_path, [(be_key(0, 1), right + left)]))
    index.refresh()
    found, totals, matched = index.find(0, 0, 0, {"minecraft:gold_ingot"}, 5, None)
    assert matched == 1 and totals == {"minecraft:gold_ingot": 12}
    assert (found[0]["x"], found[0]["z"]) == (10, 20)


def test_unopened_loot_and_unreadable_records_are_skipped_and_counted(
    tmp_path: Path,
) -> None:
    loot = block_entity(
        "Chest", 1, 40, 1, LootTable=S("loot_tables/chests/village.json")
    )
    opened = block_entity(
        "Barrel",
        2,
        40,
        2,
        [item("minecraft:bread", 3)],
        LootTable=S("loot_tables/chests/village.json"),
    )
    good = block_entity("Hopper", 100, 64, 100, [item("minecraft:bread", 1)])
    index = containers.ContainerIndex(
        _world(
            tmp_path,
            [
                (be_key(0, 0), loot + opened),
                (be_key(1, 1), b"\x0a\x00\x00\x08\x04\x00name\xff\xff"),  # cut short
                (be_key(6, 6), good),
            ],
        )
    )
    index.refresh()
    assert index.unopened == 1 and index.unreadable == 1 and index.records == 3
    found, totals, _ = index.find(0, 0, 0, {"minecraft:bread"}, 5, None)
    assert totals == {"minecraft:bread": 4}
    assert [h["kind"] for h in found] == ["barrel", "hopper"]


def test_each_dimension_has_its_own_and_radius_and_limit_hold(tmp_path: Path) -> None:
    nether = block_entity("Chest", -80, 40, -80, [item("minecraft:quartz", 9)])
    near = block_entity("Barrel", 30, 64, 0, [item("minecraft:quartz", 1)])
    far = block_entity("Barrel", 3000, 64, 0, [item("minecraft:quartz", 2)])
    path = tmp_path / "db"
    path.mkdir()
    write_table(
        path / "000005.ldb", [(be_key(-5, -5, 1), 1, nether), (be_key(1, 0), 2, near)]
    )
    write_log(path / "000006.log", 10, [(be_key(187, 0), far)])
    index = containers.ContainerIndex(path)
    index.refresh()
    assert index.find(1, 0, 0, {"minecraft:quartz"}, 5, None)[1] == {
        "minecraft:quartz": 9
    }
    found, totals, matched = index.find(0, 0, 0, {"minecraft:quartz"}, 5, 100)
    assert matched == 1 and found[0]["x"] == 30
    found, totals, matched = index.find(0, 0, 0, {"minecraft:quartz"}, 1, None)
    assert matched == 2 and len(found) == 1 and totals == {"minecraft:quartz": 3}


def test_the_index_rereads_only_when_the_files_change(tmp_path: Path) -> None:
    clock = [0.0]
    db = _world(
        tmp_path, [(be_key(0, 0), block_entity("Chest", 1, 1, 1, [item("apple", 1)]))]
    )
    index = containers.ContainerIndex(db, clock=lambda: clock[0])
    index.refresh()
    assert index.names(0) == {"minecraft:apple"}
    write_log(db / "000011.log", 200, [(be_key(0, 0), None)])  # the chest broken
    index.refresh()
    assert index.names(0) == {"minecraft:apple"}  # inside REREAD_S: not looked at
    index.refresh(force=True)
    assert index.names(0) == set()
    clock[0] = containers.REREAD_S + 1
    write_log(
        db / "000012.log",
        300,
        [(be_key(0, 0), block_entity("Chest", 1, 1, 1, [item("pear", 1)]))],
    )
    index.refresh()
    assert index.names(0) == {"minecraft:pear"}
