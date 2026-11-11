"""A phone's visual-memory record is checked before the box keeps it (plan §2c, wave G4d):
refused, never cleaned, so a record the box stored is byte for byte what the phone compares."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from jbrain.growbot.memories import MAX_VIEW_B64, MemoryRefused, from_phone
from jbrain.storage import FsBlobStore

JPEG = b"\xff\xd8\xff\xe0pixels"


def _record(**over: Any) -> dict[str, Any]:
    return {
        "id": "r0a1b2",
        "kind": "person",
        "name": "my person",
        "desc": "",
        "t": 1762000000000,
        "confirmed": "agent",
        "namedAt": 0,
        "views": [{"j": base64.b64encode(JPEG).decode(), "t": 1762000000000}],
        **over,
    }


def test_a_record_is_kept_exactly_as_the_creature_wrote_it() -> None:
    saved = from_phone(_record(kind="thing", t=1.5, v=1, key="ns/r0a1b2"))
    assert saved.id == "r0a1b2"
    # Only the creature's own fields; the phone's key and namespace are its own business.
    assert saved.meta == {
        "kind": "thing",
        "name": "my person",
        "desc": "",
        "t": 1.5,
        "confirmed": "agent",
        "namedAt": 0,
    }
    assert saved.views == [(JPEG, 1762000000000)]


@pytest.mark.parametrize(
    "over",
    [
        {"id": "x1"},
        {"id": 7},
        {"kind": "pet"},
        {"name": None},
        {"name": "n" * 61},
        {"desc": "d" * 201},
        {"desc": "half an emoji \ud83c"},
        {"name": "nul\x00"},
        {"confirmed": ""},
        {"t": float("nan")},
        {"t": "now"},
        {"namedAt": False},
        {"views": []},
        {"views": [{"j": base64.b64encode(JPEG).decode(), "t": 1}] * 4},
        {"views": ["x"]},
        {"views": [{"j": "", "t": 1}]},
        {"views": [{"j": "A" * (MAX_VIEW_B64 + 4), "t": 1}]},
        {"views": [{"j": "!!!!", "t": 1}]},
        {"views": [{"j": base64.b64encode(b"\x89PNG....").decode(), "t": 1}]},
        {"views": [{"j": base64.b64encode(JPEG).decode(), "t": None}]},
    ],
)
def test_anything_else_is_refused(over: dict[str, Any]) -> None:
    with pytest.raises(MemoryRefused):
        from_phone(_record(**over))


def test_a_record_must_be_an_object() -> None:
    with pytest.raises(MemoryRefused):
        from_phone(["r1"])


async def test_a_namespace_is_its_own_store(tmp_path: Path) -> None:
    main = FsBlobStore(tmp_path)
    faces = main.namespace("growbot-memories")
    sha = await faces.put(JPEG)
    assert await faces.exists(sha) and not await main.exists(sha)
    assert faces.path_for(sha).is_relative_to(tmp_path / "ns-growbot-memories")
    for bad in ("", "../up", "Growbot", "a/b", "9lives"):
        with pytest.raises(ValueError):
            main.namespace(bad)


def test_a_new_namespace_reports_the_volume_it_will_live_on(tmp_path: Path) -> None:
    faces = FsBlobStore(tmp_path / "blobs").namespace("growbot-memories")
    # Neither it nor the store above it exists yet: still a real answer, never "no room".
    assert faces.free_bytes() > 0
