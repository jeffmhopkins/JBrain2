"""The desk panel's settings between the PWA and the glass (firmware-c6 0.0.21), with no I/O.

What is pinned: the pickup's report is read whole or not at all; the owner's request is checked
against the panel's own ranges; and the owner's rule for a race (2026-10-13: "the panel's last
report wins") — a request settles on its value, waits while the panel still reports the value it
was asked over, and is dropped when the panel reports anything else.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jbrain.jpanel import panel_settings as S
from jbrain.jpanel.notice import PANEL_ZONES

_FW = Path(__file__).resolve().parents[3] / "firmware-c6" / "components"

FULL = {
    "na": "1",
    "nq": "1",
    "nc": "0",
    "nf": "1290",
    "nt": "390",
    "tz": "5",
    "no": "1",
    "nk": "0",
    "sd": "1",
    "br": "50",
    "vo": "80",
    "ra": "1",
    "ar": "0",
}


def test_a_full_report_is_read_by_name() -> None:
    got = S.parse_report(dict(FULL))
    assert got == {
        "notif_accept": 1,
        "notif_quiet": 1,
        "notif_chime": 0,
        "night_from": 1290,
        "night_to": 390,
        "tz": 5,
        "night_on": 1,
        "night_color": 0,
        "show_date": 1,
        "brightness": 50,
        "volume": 80,
        "read_aloud": 1,
        "auto_rotate": 0,
    }


def test_a_panel_with_no_notice_surface_still_reports_the_rest() -> None:
    """A panel whose `storage` did not mount reports no notice switches (0.0.19's rule); its
    screen and sound settings still reach the PWA."""
    core = {k: v for k, v in FULL.items() if k not in S.NOTICE_WIRES}
    got = S.parse_report(core)
    assert got is not None and set(got) == {
        "night_on",
        "night_color",
        "show_date",
        "brightness",
        "volume",
        "read_aloud",
        "auto_rotate",
    }


@pytest.mark.parametrize(
    "params",
    [
        {k: v for k, v in FULL.items() if k != "br"},  # firmware before 0.0.21
        {**FULL, "br": "5"},  # under the floor: not what a panel that clamps would say
        {**FULL, "vo": "101"},
        {**FULL, "nk": "3"},
        {**FULL, "tz": str(len(PANEL_ZONES))},
        {**FULL, "ar": "x"},
        {"board": "c6-amoled-216"},
    ],
)
def test_a_report_that_is_not_whole_or_not_sane_is_none(params: dict[str, str]) -> None:
    assert S.parse_report(params) is None


def test_the_owner_s_request_is_checked_against_the_panel_s_ranges() -> None:
    assert S.check({"brightness": 60, "night_on": False, "tz": 0}) == {
        "brightness": 60,
        "night_on": 0,
        "tz": 0,
    }
    for bad in (
        {"brightness": 9},
        {"volume": 101},
        {"night_from": 1440},
        {"night_color": -1},
        {"mic_gain": 20},  # stays in the debug console
        {"imu_axes": 1},
        {"brightness": "60"},
        {"brightness": 60.5},
    ):
        with pytest.raises(S.BadSetting):
            S.check(bad)


def test_a_request_remembers_what_the_panel_held_and_withdraws_on_a_return() -> None:
    reported = {"brightness": 50, "night_color": 0}
    q = S.request({}, reported, {"brightness": 60, "night_color": 1})
    assert q == {"brightness": {"value": 60, "base": 50}, "night_color": {"value": 1, "base": 0}}
    # Asked again before the panel took it: the newer value, over what the panel still holds.
    q = S.request(q, reported, {"brightness": 70})
    assert q["brightness"] == {"value": 70, "base": 50}
    # Asking for what the panel already holds takes the request back.
    assert S.request(q, reported, {"brightness": 50}) == {"night_color": {"value": 1, "base": 0}}
    with pytest.raises(KeyError):
        S.request({}, {"brightness": 50}, {"volume": 60})


def test_a_report_settles_keeps_or_drops_each_request() -> None:
    q = {
        "brightness": {"value": 60, "base": 50},  # the panel took it
        "volume": {"value": 60, "base": 80},  # still the old value: offline, or not yet fetched
        "night_color": {"value": 1, "base": 0},  # changed at the glass to grey meanwhile
        "tz": {"value": 2, "base": 5},  # not in this report (no notice surface): waits
    }
    keep, settled, dropped = S.reconcile(q, {"brightness": 60, "volume": 80, "night_color": 2})
    assert keep == {"volume": {"value": 60, "base": 80}, "tz": {"value": 2, "base": 5}}
    assert settled == ["brightness"] and dropped == ["night_color"]
    assert S.reconcile({}, {"brightness": 60}) == ({}, [], [])


def test_the_panel_s_copy_is_short_names_value_and_base() -> None:
    q = {"night_color": {"value": 1, "base": 0}, "brightness": {"value": 60, "base": 50}}
    assert S.wire_pending(7, q) == {"v": 7, "s": {"br": [60, 50], "nk": [1, 0]}}
    assert S.wire_pending(0, {}) == {"v": 0, "s": {}}


def test_the_ranges_and_short_names_are_the_firmware_s() -> None:
    """The panel clamps every write (`svc_settings_clamp`) and builds the report
    (`settings_sync.c`); a range or a name that drifts here would queue a request the panel
    clamps to something else, or read a report it never sends."""
    header = (_FW / "app_api" / "include" / "svc_settings.h").read_text()

    def define(name: str) -> int:
        m = re.search(rf"#define {name} (\d+)", header)
        assert m, name
        return int(m.group(1))

    assert (S.BY_NAME["brightness"].lo, S.BY_NAME["brightness"].hi) == (
        define("BRIGHTNESS_FLOOR"),
        define("BRIGHTNESS_MAX"),
    )
    assert (S.BY_NAME["volume"].lo, S.BY_NAME["volume"].hi) == (
        define("VOLUME_FLOOR"),
        define("VOLUME_MAX"),
    )
    sync = (_FW / "svc_settings" / "settings_sync.c").read_text()

    def table(name: str) -> set[str]:
        body = sync.split(f"{name}[", 1)[1].split("};", 1)[0]
        return set(re.findall(r'\{SET_[A-Z_]+, "([a-z]{2})"\}', body))

    assert table("REPORT") == set(S.CORE_WIRES), "the pickup reports exactly the core settings"
    assert table("SYNC_KEYS") == {s.wire for s in S.SETTINGS}, "and applies what the PWA may ask"
