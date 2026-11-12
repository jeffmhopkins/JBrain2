"""A persona's settings as the box keeps them (growbot/prefs.py)."""

from __future__ import annotations

from typing import Any

import pytest

from jbrain.growbot import prefs


def test_a_patch_sets_and_drops_the_creatures_keys() -> None:
    patch = prefs.patch_from(
        {"gb_vol": "0.5", "gb_vismem_native/gb-abc": "0", "gb_ft_latch:gb-a1b2c3": "1"},
        ["gb_eye"],
    )
    assert prefs.merged({"gb_eye": "environment", "gb_stt": "di"}, patch) == {
        "gb_stt": "di",
        "gb_vol": "0.5",
        "gb_vismem_native/gb-abc": "0",
        "gb_ft_latch:gb-a1b2c3": "1",
    }
    # Nothing sent is nothing changed; a key never set drops quietly.
    assert prefs.merged(None, prefs.patch_from(None, ["gb_vol"])) == {}


@pytest.mark.parametrize(
    ("set_", "drop"),
    [
        (["gb_vol"], []),
        ({}, "gb_vol"),
        ({"": "1"}, []),
        ({"../x": "1"}, []),
        ({"a" * 81: "1"}, []),
        ({"gb_vol": 1}, []),
        ({"gb_vol": None}, []),
        ({"gb_vol": "a\x00b"}, []),
        ({"gb_vol": "\ud800"}, []),
        ({}, [1]),
        ({}, ["has space"]),
    ],
)
def test_a_patch_that_is_not_settings_is_refused(set_: Any, drop: Any) -> None:
    with pytest.raises(prefs.PrefsRefused) as caught:
        prefs.patch_from(set_, drop)
    assert not isinstance(caught.value, prefs.PrefsTooBig)


def test_settings_past_the_caps_are_too_big() -> None:
    with pytest.raises(prefs.PrefsTooBig):
        prefs.patch_from({"gb_next_games3": "x" * (prefs.VALUE_MAX + 1)}, [])
    with pytest.raises(prefs.PrefsTooBig):
        prefs.patch_from({f"k{i}": "1" for i in range(prefs.MAX_KEYS + 1)}, [])
    big = {f"k{i}": "x" * 10_000 for i in range(7)}
    with pytest.raises(prefs.PrefsTooBig):
        prefs.merged(big, prefs.patch_from({"k8": "x" * 10_000}, []))
    many = {f"k{i}": "1" for i in range(prefs.MAX_KEYS)}
    with pytest.raises(prefs.PrefsTooBig):
        prefs.merged(many, prefs.patch_from({"one_more": "1"}, []))
    # Values the box already holds that are not text are never handed back.
    assert prefs.merged({"odd": 1}, prefs.patch_from({}, [])) == {}


def test_the_owner_only_mark_takes_no_settings_room() -> None:
    full = {f"k{i}": "1" for i in range(prefs.MAX_KEYS - 1)}
    got = prefs.merged({prefs.OWNER_ONLY: "1", **full}, prefs.patch_from({"last": "1"}, []))
    assert len(got) == prefs.MAX_KEYS + 1
    assert prefs.for_phone(got) == ({**full, "last": "1"}, True)
    assert prefs.for_phone(None) == (None, False)
