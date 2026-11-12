"""The creature's system prefix on disk (jbrain.growbot.prefix, and the lane's use of it in
api/growbot_llm.py): a cold turn saves it once the creature is quiet, a turn after a long
quiet restores it, and nothing ever fails a turn. A fake store and a faked gateway."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jbrain.growbot import prefix
from jbrain.llm import prefill
from jbrain.llm.slot_roles import GROWBOT_ROLE
from tests.unit.test_growbot_llm import _COMPLETIONS, _app, _chat, _Gateway

_SYSTEM = "be a small curious robot. " * 800


class _Store:
    def __init__(self, restored: bool = True, saved: bool = True, fail: bool = False) -> None:
        self.restores: list[tuple[str, str, Any]] = []
        self.saves: list[tuple[str, str, int, Any]] = []
        self._restored = restored
        self._saved = saved
        self._fail = fail

    async def restore_if_lost(
        self, served_model: str, system: str, tools: list[Any], *, role: Any = None
    ) -> bool:
        if self._fail:
            raise RuntimeError("gateway down")
        self.restores.append((served_model, system, role))
        return self._restored

    async def save_after_prime(
        self,
        served_model: str,
        system: str,
        tools: list[Any],
        prime_tokens: int,
        *,
        role: Any = None,
    ) -> bool:
        self.saves.append((served_model, system, prime_tokens, role))
        return self._saved


@pytest.fixture(autouse=True)
def _fresh_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefill, "_ratio", {})


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefix, "SAVE_QUIET_S", 0.0)
    monkeypatch.setattr(prefix, "_POLL_S", 0.01)


def _usage(tokens: int) -> tuple[bytes, ...]:
    return (json.dumps({"choices": [], "usage": {"prompt_tokens": tokens}}).encode(),)


def _cold(*, cached: int = 0) -> tuple[bytes, ...]:
    """A streamed reply whose engine timings say how much of the prompt was reused."""
    events = (
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [], "timings": {"cache_n": cached, "prompt_n": 7000}},
    )
    return (*(f"data: {json.dumps(e)}\n\n".encode() for e in events), b"data: [DONE]\n\n")


class _Lane(_Gateway):
    """Streams `chunks` to a turn and answers a prime (one output token) with its usage."""

    def stream(self, method: str, path: str, *, json: dict[str, Any]) -> Any:
        if json.get("max_tokens") == 1 and json.get("stream") is False:
            return _Gateway(chunks=_usage(7100)).stream(method, path, json=json)
        return super().stream(method, path, json=json)


def _turn(stream: bool = True) -> dict[str, Any]:
    body = _chat(content="hello", stream=stream)
    body["messages"].insert(0, {"role": "system", "content": _SYSTEM})
    return body


async def test_a_restore_asks_the_store_for_the_growbot_slot() -> None:
    store = _Store()
    assert await prefix.restore(store, "m", _SYSTEM) is True
    assert store.restores == [("m", _SYSTEM, GROWBOT_ROLE)]


async def test_a_short_system_or_no_store_is_never_restored() -> None:
    store = _Store()
    assert await prefix.restore(store, "m", "tiny") is False
    assert await prefix.restore(None, "m", _SYSTEM) is False
    assert store.restores == []


async def test_a_failing_restore_is_a_cold_turn_not_an_error() -> None:
    assert await prefix.restore(_Store(fail=True), "m", _SYSTEM) is False


async def test_a_save_primes_the_slot_then_saves_by_its_token_count() -> None:
    store = _Store()
    gateway = _Gateway(chunks=_usage(7123))
    saver = prefix.PrefixSaver()
    levels: list[dict[str, Any]] = []
    assert saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        key="k",
        last_turn=lambda: None,
        lock=asyncio.Lock(),
        pool_guard=None,
        client_factory=gateway,
        apply_level=levels.append,
    )
    assert saver._task is not None
    await saver._task
    (sent,) = gateway.sent
    assert sent["json"]["messages"] == [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "."},
    ]
    assert sent["json"]["max_tokens"] == 1 and sent["json"]["stream"] is False
    assert levels == [sent["json"]]
    assert store.saves == [("m", _SYSTEM, 7123, GROWBOT_ROLE)]
    assert saver.saved == {"k"} and gateway.closed == 1
    # Saved once per system text: the same key schedules nothing.
    assert not saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        key="k",
        last_turn=lambda: None,
        lock=None,
        pool_guard=None,
        client_factory=gateway,
        apply_level=levels.append,
    )


@pytest.mark.parametrize(
    "chunks", [(b"not json",), (b'{"usage": {"prompt_tokens": "x"}}',), (b"[]",)]
)
async def test_an_unreadable_prime_saves_nothing(chunks: tuple[bytes, ...]) -> None:
    store = _Store()
    saver = prefix.PrefixSaver()
    saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        key="k",
        last_turn=lambda: None,
        lock=None,
        pool_guard=None,
        client_factory=_Gateway(chunks=chunks),
        apply_level=lambda _b: None,
    )
    assert saver._task is not None
    await saver._task
    assert store.saves == [] and saver.saved == set()


async def test_a_refused_prime_saves_nothing() -> None:
    store = _Store()
    saver = prefix.PrefixSaver()
    saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        key="k",
        last_turn=lambda: None,
        lock=None,
        pool_guard=None,
        client_factory=_Gateway(chunks=_usage(7000), status=503),
        apply_level=lambda _b: None,
    )
    assert saver._task is not None
    await saver._task
    assert store.saves == []


async def test_a_save_waits_for_quiet_and_gives_up_on_chatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prefix, "SAVE_QUIET_S", 60.0)
    monkeypatch.setattr(prefix, "SAVE_GIVE_UP_S", 0.05)
    store = _Store()
    gateway = _Gateway(chunks=_usage(7000))
    saver = prefix.PrefixSaver()
    saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        key="k",
        last_turn=time.monotonic,  # the creature never goes quiet
        lock=None,
        pool_guard=None,
        client_factory=gateway,
        apply_level=lambda _b: None,
    )
    assert saver.busy()
    assert saver._task is not None
    await saver._task
    assert gateway.sent == [] and store.saves == []


async def test_a_failing_save_is_only_a_log_line() -> None:
    class _Broken(_Store):
        async def save_after_prime(self, *_a: Any, **_k: Any) -> bool:
            raise RuntimeError("disk full")

    saver = prefix.PrefixSaver()
    saver.schedule(
        store=_Broken(),
        model="m",
        system=_SYSTEM,
        key="k",
        last_turn=lambda: None,
        lock=None,
        pool_guard=None,
        client_factory=_Gateway(chunks=_usage(7000)),
        apply_level=lambda _b: None,
    )
    assert saver._task is not None
    await saver._task
    assert saver.saved == set()


def test_short_systems_and_a_busy_saver_schedule_nothing() -> None:
    saver = prefix.PrefixSaver()

    def go(system: str, store: Any) -> bool:
        return saver.schedule(
            store=store,
            model="m",
            system=system,
            key=system[:5],
            last_turn=lambda: None,
            lock=None,
            pool_guard=None,
            client_factory=_Gateway(),
            apply_level=lambda _b: None,
        )

    assert not go("tiny", _Store())
    assert not go(_SYSTEM, None)


# --- the lane ------------------------------------------------------------------------------


def test_a_first_turn_restores_and_a_close_one_does_not(tmp_path: Path) -> None:
    store = _Store()
    app, auth, _gw = _app(tmp_path, gateway=_Gateway(chunks=_cold(cached=7000)))
    app.state.kv_prefix = store
    with TestClient(app) as client:
        assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
        assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
    assert len(store.restores) == 1 and store.restores[0][1] == _SYSTEM
    app.state.growbot_last_turn = time.monotonic() - prefix.RESTORE_AFTER_QUIET_S - 1
    with TestClient(app) as client:
        assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
    assert len(store.restores) == 2


def test_a_cold_turn_schedules_a_save_and_a_warm_one_does_not(tmp_path: Path) -> None:
    store = _Store()
    app, auth, _gw = _app(tmp_path, gateway=_Gateway(chunks=_cold(cached=7000)))
    app.state.kv_prefix = store
    with TestClient(app) as client:
        assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
    assert getattr(app.state, "growbot_prefix_saver", None) is None
    app.state.growbot_llm_client_factory = _Lane(chunks=_cold(cached=0))
    with TestClient(app) as client:
        assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
        saver = app.state.growbot_prefix_saver
        deadline = time.monotonic() + 2
        while saver.busy() and time.monotonic() < deadline:
            time.sleep(0.01)
    assert len(store.saves) == 1
    model, system, _tokens, role = store.saves[0]
    assert system == _SYSTEM and role == GROWBOT_ROLE and model


def test_a_turn_marks_the_creature_awake_at_most_once_a_minute(tmp_path: Path) -> None:
    from tests.unit.fakes import FakeSettingsStore

    store = FakeSettingsStore()
    app, auth, _gw = _app(tmp_path, gateway=_Gateway(chunks=_cold(cached=7000)))
    app.state.settings_store = store
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
    assert store.values["growbot_awake_marks"] == 1
