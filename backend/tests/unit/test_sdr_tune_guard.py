"""The hole in the tuner's range, and every door that has to know about it.

14.4-24 MHz passes every bound the api declares — it is above `TUNABLE_MIN_MHZ` and
below `MAX_MHZ` — and the radio cannot receive it. Below 24 MHz the sidecar tunes with
`-E direct2`, and direct sampling digitises a real signal at 28.8 MS/s, so the second
Nyquist zone folds onto the first: ask for 18.1 MHz and 10.7 MHz arrives instead. There
is no error anywhere in that chain. The audio plays, the level meter reads, whisper
transcribes, and the 31 m broadcast band gives the listener every reason to believe
what they hear (docs/plans/SDR_IQ_SPECTRUM_PLAN.md §8).

So the test is per DOOR, not per predicate: the failure this class of bug produces is
one entry point that never asked, and the only way to see that is to try them all.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.api import debug as debug_api
from jbrain.api import sdr as sdr_api
from jbrain.sdr.tuner import MIN_MHZ, NYQUIST_MHZ, aliased, out_of_range

#: The example the plan names, and the reason it is the example: 10.7 MHz is inside the
#: 31 m broadcast band, so what comes back is a station rather than noise.
ASKED_MHZ = 18.1
RECEIVED_MHZ = 10.7

OWNER: Any = SimpleNamespace(id="owner", kind="owner")


class _Settings:
    sdr_url = "http://sdr:8000"
    supervisor_token = "t"


def _settings() -> Any:
    return _Settings()


def _request() -> Any:
    return SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()))


@pytest.fixture(autouse=True)
def _no_radio_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every path out of these routes fails loudly, so a refusal that arrives AFTER the
    radio was taken cannot pass as a refusal. The whole point is that the guard runs
    before anything holds a dongle."""

    async def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("an unreceivable frequency reached the radio")

    for module in (sdr_api, debug_api):
        monkeypatch.setattr(module, "httpx", _Exploding(), raising=False)
    monkeypatch.setattr(sdr_api, "_post", never)
    monkeypatch.setattr(sdr_api, "_radio_for", never)
    monkeypatch.setattr(sdr_api, "_health", never)
    monkeypatch.setattr(debug_api, "_sdr_post", never)
    monkeypatch.setattr(debug_api, "_radio", never)


class _Exploding:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"an unreceivable frequency reached httpx.{name}")


def _said(raised: pytest.ExceptionInfo[HTTPException]) -> str:
    assert raised.value.status_code == 400
    return str(raised.value.detail)


# --- the predicate ------------------------------------------------------------------


def test_the_alias_is_named_by_the_frequency_that_really_arrives() -> None:
    """ "Out of range" would be a lie — the radio tunes it happily. The fact the owner
    needs is which frequency they would have been listening to."""
    said = aliased(ASKED_MHZ)

    assert said and f"{RECEIVED_MHZ:g} MHz" in said
    assert said == out_of_range(ASKED_MHZ)  # one predicate answers "can I tune this"


def test_the_hole_is_exactly_the_second_nyquist_zone() -> None:
    # Both edges are reachable: 14.4 is the last honest direct-path frequency and 24 is
    # where the tuner takes over. The hole is strictly between them.
    assert aliased(NYQUIST_MHZ) is None
    assert aliased(MIN_MHZ) is None
    assert aliased(NYQUIST_MHZ + 0.1) is not None
    assert aliased(MIN_MHZ - 0.1) is not None
    # And nothing outside it is touched by this rule.
    for mhz in (0.53, 7.2, 14.0, 24.0, 146.52, 1766.0):
        assert aliased(mhz) is None, mhz


# --- the doors ----------------------------------------------------------------------


async def test_starting_a_listening_session_refuses_before_taking_a_radio() -> None:
    with pytest.raises(HTTPException) as raised:
        await sdr_api.listen(_request(), _settings(), OWNER, ASKED_MHZ)

    assert f"{RECEIVED_MHZ:g} MHz" in _said(raised)


async def test_retuning_a_live_session_refuses_too() -> None:
    """The one the plan names, and the sharper case: a session is already running on a
    frequency the owner chose, so the picture and the audio keep working — they would
    simply be of somewhere else."""
    with pytest.raises(HTTPException) as raised:
        await sdr_api.tune(_settings(), OWNER, ASKED_MHZ)

    assert f"{RECEIVED_MHZ:g} MHz" in _said(raised)


async def test_aprs_logging_refuses_an_unreceivable_channel() -> None:
    with pytest.raises(HTTPException) as raised:
        await sdr_api.aprs_logging(_request(), _settings(), OWNER, True, ASKED_MHZ)

    assert f"{RECEIVED_MHZ:g} MHz" in _said(raised)


async def test_turning_aprs_off_is_never_blocked_by_a_frequency() -> None:
    """Stopping ignores the frequency entirely, and a guard that refused here would
    leave a radio held because of a number nobody is going to use."""

    async def health(_base: str) -> dict[str, Any]:
        return {"sessions": []}

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sdr_api, "_health", health)

        out = await sdr_api.aprs_logging(_request(), _settings(), OWNER, False, ASKED_MHZ)

    assert out == {"logging": False, "changed": False}


async def test_the_debug_listen_twin_refuses_it_as_well() -> None:
    """A capability token reaches this route and not the owner's one, so a guard on
    only the owner surface is a guard with a way around it."""
    with pytest.raises(HTTPException) as raised:
        await debug_api.sdr_listen_debug(_request(), _settings(), OWNER, ASKED_MHZ)

    assert f"{RECEIVED_MHZ:g} MHz" in _said(raised)


async def test_the_debug_capture_refuses_it_before_recording() -> None:
    """A capture from the alias transcribes cleanly and files the words under the
    frequency that was asked for, which is the worst of the three outcomes."""
    with pytest.raises(HTTPException) as raised:
        await debug_api.sdr_capture(_request(), _settings(), OWNER, ASKED_MHZ)

    assert f"{RECEIVED_MHZ:g} MHz" in _said(raised)


async def test_an_ordinary_frequency_still_gets_through() -> None:
    """The guard must not become a floor: shortwave below 14.4 MHz listens perfectly,
    and refusing it is the bug this repo already fixed once."""
    posted: list[dict[str, Any]] = []

    async def post(_settings_: Any, _path: str, body: dict[str, Any]) -> dict[str, Any]:
        posted.append(body)
        return {"session_id": "s1"}

    async def radio(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(serial="77192819", conflict=None, refusal=None)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sdr_api, "_post", post)
        patch.setattr(sdr_api, "_radio_for", radio)
        patch.setattr(sdr_api, "_refuse", lambda _c: None)

        await sdr_api.listen(_request(), _settings(), OWNER, 7.2, "usb")

    assert posted[0]["frequency_hz"] == 7_200_000
