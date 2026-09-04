"""The radios list, and the store behind it.

Neither had a test, which an independent review caught. The store's read is the more
dangerous half: it decides which antenna a service listens through, so a malformed
stored value must degrade to something safe rather than crash a tuner — and one of its
branches (an unknown role stays RESERVED) is a load-bearing decision rather than
defensive tidying.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jbrain.api import sdr as sdr_api
from jbrain.sdr.roles import GENERAL, Radio

WHIP, WIRE = "09022796", "77192819"
OWNER = SimpleNamespace(id="owner", kind="owner")


class _Store:
    """The settings store, holding whatever a test puts in it."""

    def __init__(self, radios: dict[str, Radio] | None = None) -> None:
        self.radios = radios or {}
        self.wrote: list[tuple[str, dict[str, Any]]] = []

    async def sdr_radios(self, _ctx: Any) -> dict[str, Radio]:
        return self.radios

    async def set_sdr_radio(self, _ctx: Any, serial: str, **fields: Any) -> dict[str, Radio]:
        self.wrote.append((serial, fields))
        self.radios[serial] = Radio(serial=serial, **fields)
        return self.radios

    async def forget_sdr_radio(self, _ctx: Any, serial: str) -> dict[str, Radio]:
        self.radios.pop(serial, None)
        return self.radios


class _Settings:
    sdr_url = "http://sdr:8000"
    supervisor_token = "t"


def _request(usb: dict[str, Any] | None) -> Any:
    class _Client:
        async def get(self, _path: str, **_kw: Any) -> Any:
            if usb is None:
                raise httpx.ConnectError("no supervisor")
            return httpx.Response(200, json=usb, request=httpx.Request("GET", "http://s/usb"))

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor_client=_Client())))


def _scan(*serials: str) -> dict[str, Any]:
    return {"sysfs_readable": True, "sdrs": [{"serial": s} for s in serials]}


async def _radios(
    monkeypatch: pytest.MonkeyPatch, store: _Store, usb: dict[str, Any] | None
) -> Any:
    monkeypatch.setattr(sdr_api, "get_settings_store", lambda _r: store)
    monkeypatch.setattr(sdr_api, "ctx_for", lambda _o: object())
    return await sdr_api.radios(_request(usb), _Settings(), OWNER)  # type: ignore[arg-type]


async def test_the_list_is_the_union_of_described_and_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Either alone is wrong. A dongle plugged in but never named has to appear or it is
    invisible; a dongle named but unplugged has to appear or the service waiting on it
    cannot say what it is waiting for."""
    store = _Store({WHIP: Radio(WHIP, name="Desk whip", role="aprs")})

    out = await _radios(monkeypatch, store, _scan(WIRE))

    assert [(r.serial, r.attached) for r in out.radios] == [(WHIP, False), (WIRE, True)]
    # ...and an undescribed radio arrives usable rather than blank-and-broken.
    assert out.radios[1].role == GENERAL


async def test_a_double_booked_service_is_reported_on_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settings screen shows this on the cards, before anything tries to run —
    `choose` only meets it when a service starts, which is too late to be the only
    warning."""
    store = _Store({WHIP: Radio(WHIP, role="aprs"), WIRE: Radio(WIRE, role="aprs")})

    out = await _radios(monkeypatch, store, _scan(WHIP, WIRE))

    assert out.conflicts == {"aprs": [WHIP, WIRE]}


async def test_scan_ok_says_whether_the_scan_ANSWERED_not_whether_it_found_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived from the device count, this told an owner who had simply unplugged both
    dongles that the USB scan was unreachable — and sent them debugging a healthy
    proxy."""
    store = _Store({WHIP: Radio(WHIP, name="Desk whip")})

    healthy_but_empty = await _radios(monkeypatch, store, _scan())
    unreachable = await _radios(monkeypatch, store, None)

    assert healthy_but_empty.scan_ok is True
    assert unreachable.scan_ok is False


async def test_describing_a_radio_writes_it_and_returns_the_whole_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    monkeypatch.setattr(sdr_api, "get_settings_store", lambda _r: store)
    monkeypatch.setattr(sdr_api, "ctx_for", lambda _o: object())

    out = await sdr_api.describe_radio(
        _request(_scan(WIRE)),  # type: ignore[arg-type]
        _Settings(),  # type: ignore[arg-type]
        OWNER,  # type: ignore[arg-type]
        sdr_api.RadioIn(name="Long wire", description="9:1 unun", role="aprs"),
        WIRE,
    )

    assert store.wrote == [(WIRE, {"name": "Long wire", "description": "9:1 unun", "role": "aprs"})]
    assert [(r.serial, r.name, r.role) for r in out.radios] == [(WIRE, "Long wire", "aprs")]


async def test_forgetting_a_radio_drops_it_from_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct from setting it general: a radio still on the desk and one sold last
    month are different states, and only one keeps a name."""
    store = _Store({WHIP: Radio(WHIP, name="Old dongle")})
    monkeypatch.setattr(sdr_api, "get_settings_store", lambda _r: store)
    monkeypatch.setattr(sdr_api, "ctx_for", lambda _o: object())

    out = await sdr_api.forget_radio(
        _request(_scan()),  # type: ignore[arg-type]
        _Settings(),  # type: ignore[arg-type]
        OWNER,  # type: ignore[arg-type]
        WHIP,
    )

    assert out.radios == []


class TestReadingWhatWasStored:
    """`SqlSettingsStore.sdr_radios`, defensively.

    This value decides which antenna a service listens through, so a malformed one must
    degrade to the behaviour of a box that has never opened the screen — not crash a
    tuner. The load-bearing branch is the last one: an UNKNOWN role stays reserved.
    """

    def _store(self, stored: Any) -> Any:
        from jbrain.settings_store import SqlSettingsStore

        store = SqlSettingsStore.__new__(SqlSettingsStore)

        async def _get(_ctx: Any, _key: str, default: Any = None) -> Any:
            return stored

        store.get = _get  # type: ignore[method-assign]
        return store

    async def test_it_reads_what_the_owner_wrote(self) -> None:
        radios = await self._store(
            {WIRE: {"name": "Long wire", "description": "9:1 unun", "role": "aprs"}}
        ).sdr_radios(object())

        assert radios[WIRE] == Radio(WIRE, "Long wire", "9:1 unun", "aprs")

    async def test_junk_degrades_to_no_radios_rather_than_raising(self) -> None:
        for junk in ("nope", 5, None, ["a"]):
            assert await self._store(junk).sdr_radios(object()) == {}

    async def test_a_bad_entry_is_dropped_and_the_rest_survive(self) -> None:
        radios = await self._store(
            {WHIP: "not a dict", "": {"name": "x"}, WIRE: {"name": "Long wire"}}
        ).sdr_radios(object())

        assert list(radios) == [WIRE]

    async def test_bad_fields_fall_back_one_at_a_time(self) -> None:
        radios = await self._store({WIRE: {"name": 5, "description": None}}).sdr_radios(object())

        assert radios[WIRE] == Radio(WIRE, "", "", GENERAL)

    async def test_an_unknown_role_stays_RESERVED_rather_than_becoming_general(self) -> None:
        """The decision, not defensive tidying.

        A role this build does not recognise means the radio is dedicated to a service a
        NEWER build has. Coercing it to general would hand that radio to the tuner —
        silently freeing a reservation because we could not read it, which is the exact
        failure the feature exists to prevent."""
        radios = await self._store({WIRE: {"role": "shortwave"}}).sdr_radios(object())

        assert radios[WIRE].role == "shortwave"

    async def test_long_values_are_bounded(self) -> None:
        radios = await self._store(
            {WIRE: {"name": "n" * 500, "description": "d" * 900, "role": "r" * 300}}
        ).sdr_radios(object())

        assert len(radios[WIRE].name) == 60
        assert len(radios[WIRE].description) == 200
        assert len(radios[WIRE].role) == 40
