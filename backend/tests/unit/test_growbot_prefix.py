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
from jbrain.llm import local_catalog, prefill
from jbrain.llm.slot_roles import GROWBOT_ROLE
from tests.unit.test_growbot_llm import _COMPLETIONS, _app, _chat, _Gateway

_SYSTEM = "be a small curious robot. " * 800
# The real check, kept before the autouse fixture stands every model in a pool.
_REAL_POOLED = prefix._pooled


class _Store:
    def __init__(self, restored: bool = True, saved: bool = True, fail: bool = False) -> None:
        self.restores: list[tuple[str, str, Any]] = []
        self.saves: list[tuple[str, str, int, Any]] = []
        self.efforts: list[str | None] = []
        self.used: list[tuple[str, str | None, Any]] = []
        self._restored = restored
        self._saved = saved
        self._fail = fail

    async def restore_if_lost(
        self,
        served_model: str,
        system: str,
        tools: list[Any],
        *,
        reasoning_effort: str | None = None,
        role: Any = None,
    ) -> bool:
        if self._fail:
            raise RuntimeError("gateway down")
        self.restores.append((served_model, system, role))
        self.efforts.append(reasoning_effort)
        return self._restored

    async def save_after_prime(
        self,
        served_model: str,
        system: str,
        tools: list[Any],
        prime_tokens: int,
        *,
        reasoning_effort: str | None = None,
        role: Any = None,
    ) -> bool:
        self.saves.append((served_model, system, prime_tokens, role))
        self.efforts.append(reasoning_effort)
        return self._saved

    def identity_of(
        self, served_model: str, system: str, tools: list[Any], reasoning_effort: str | None
    ) -> str | None:
        return f"{served_model}:{reasoning_effort}:{len(system)}"

    def note_prefix_used(
        self, served_model: str, fingerprint: str | None, *, role: Any = None
    ) -> None:
        self.used.append((served_model, fingerprint, role))


@pytest.fixture(autouse=True)
def _fresh_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefill, "_ratio", {})


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefix, "SAVE_QUIET_S", 0.0)
    monkeypatch.setattr(prefix, "_POLL_S", 0.01)


@pytest.fixture(autouse=True)
def _pooled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefix, "_pooled", lambda _model: True)


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
    assert await prefix.restore(store, "m", _SYSTEM, "off") is True
    assert store.restores == [("m", _SYSTEM, GROWBOT_ROLE)] and store.efforts == ["off"]


async def test_a_short_system_or_no_store_is_never_restored() -> None:
    store = _Store()
    assert await prefix.restore(store, "m", "tiny", "off") is False
    assert await prefix.restore(None, "m", _SYSTEM, "off") is False
    assert store.restores == []


async def test_a_model_without_a_role_pool_never_touches_the_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unpooled, the store would key the restore memo by model alone, sharing jerv's.
    monkeypatch.setattr(prefix, "_pooled", lambda _model: False)
    store = _Store()
    assert await prefix.restore(store, "m", _SYSTEM, "off") is False
    saver = prefix.PrefixSaver()
    assert not saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        level="off",
        key="k",
        last_turn=lambda: None,
        lock=None,
        pool_guard=None,
        client_factory=_Gateway(),
        apply_level=lambda _b: None,
    )
    assert store.restores == [] and store.saves == []


def test_the_real_pool_check_reads_the_catalog() -> None:
    from jbrain.llm import local_catalog

    pooled = [m.served_model for m in local_catalog.CATALOG if m.kv_pool is not None]
    assert pooled and _REAL_POOLED(pooled[0])
    assert not _REAL_POOLED("no-such-model")


def test_a_used_restore_clears_the_stores_memo() -> None:
    store = _Store()
    prefix.consumed(store, "m", _SYSTEM, "off")
    assert store.used == [("m", f"m:off:{len(_SYSTEM)}", GROWBOT_ROLE)]
    prefix.consumed(None, "m", _SYSTEM, "off")


def test_a_broken_memo_is_only_a_log_line() -> None:
    class _Broken(_Store):
        def note_prefix_used(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("boom")

    prefix.consumed(_Broken(), "m", _SYSTEM, "off")


async def test_a_failing_restore_is_a_cold_turn_not_an_error() -> None:
    assert await prefix.restore(_Store(fail=True), "m", _SYSTEM, "off") is False


async def test_a_save_primes_the_slot_then_saves_by_its_token_count() -> None:
    store = _Store()
    gateway = _Gateway(chunks=_usage(7123))
    saver = prefix.PrefixSaver()
    levels: list[dict[str, Any]] = []
    assert saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        level="off",
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
    assert store.saves == [("m", _SYSTEM, 7123, GROWBOT_ROLE)] and store.efforts == ["off"]
    assert saver.saved == {"k"} and gateway.closed == 1
    # Saved once per system text: the same key schedules nothing.
    assert not saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        level="off",
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
        level="off",
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


async def test_a_declined_save_is_not_primed_again() -> None:
    store = _Store(saved=False)
    saver = prefix.PrefixSaver()
    args: dict[str, Any] = {
        "store": store,
        "model": "m",
        "system": _SYSTEM,
        "level": "off",
        "key": "k",
        "last_turn": lambda: None,
        "lock": None,
        "pool_guard": None,
        "client_factory": _Gateway(chunks=_usage(7000)),
        "apply_level": lambda _b: None,
    }
    assert saver.schedule(**args)
    assert saver._task is not None
    await saver._task
    assert len(store.saves) == 1 and not saver.schedule(**args)


async def test_a_refused_prime_saves_nothing() -> None:
    store = _Store()
    saver = prefix.PrefixSaver()
    saver.schedule(
        store=store,
        model="m",
        system=_SYSTEM,
        level="off",
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
        level="off",
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
        level="off",
        key="k",
        last_turn=lambda: None,
        lock=None,
        pool_guard=None,
        client_factory=_Gateway(chunks=_usage(7000)),
        apply_level=lambda _b: None,
    )
    assert saver._task is not None
    await saver._task
    assert saver.saved == {"k"}


def test_short_systems_and_a_busy_saver_schedule_nothing() -> None:
    saver = prefix.PrefixSaver()

    def go(system: str, store: Any) -> bool:
        return saver.schedule(
            store=store,
            model="m",
            system=system,
            level="off",
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
    # The turn that ran on the restore released the store's memo.
    assert [u[2] for u in store.used] == [GROWBOT_ROLE]
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


def test_a_warm_up_does_not_hold_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A room noise can start a warm-up, so only a turn the creature answers marks it awake."""
    from tests.unit.fakes import FakeSettingsStore

    pool = next(m.kv_pool for m in local_catalog.CATALOG if m.kv_pool is not None)
    monkeypatch.setattr(local_catalog, "pool_of", lambda _model: pool)

    store = FakeSettingsStore()
    app, auth, _gw = _app(tmp_path, gateway=_Gateway(chunks=_cold(cached=7000)))
    app.state.settings_store = store
    warm = {**auth, "X-Growbot-Warm": "1"}
    with TestClient(app) as client:
        assert client.post(_COMPLETIONS, json=_turn(), headers=warm).status_code == 200
        assert "growbot_awake_marks" not in store.values
        assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
    assert store.values["growbot_awake_marks"] == 1


def test_a_long_awake_stretch_gives_the_worker_a_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jbrain.api import growbot_llm
    from tests.unit.fakes import FakeSettingsStore

    clock = [1000.0]
    monkeypatch.setattr(growbot_llm.time, "monotonic", lambda: clock[0])
    store = FakeSettingsStore()
    app, _auth, _gw = _app(tmp_path, gateway=_Gateway())
    app.state.settings_store = store

    class _Req:
        pass

    req = _Req()
    req.app = app  # type: ignore[attr-defined]

    def marks() -> int:
        value = store.values["growbot_awake_marks"]
        assert isinstance(value, int)
        return value

    async def turns(until: float) -> None:
        while clock[0] < until:
            growbot_llm._mark_awake(req)  # type: ignore[arg-type]
            await asyncio.sleep(0)
            clock[0] += 61.0

    async def run() -> None:
        await turns(1000.0 + growbot_llm._AWAKE_HOLD_MAX_S)
        held = marks()
        assert held == 20  # one a minute for the 20-minute cap
        # A turn a minute through the break stamps nothing; the worker gets its window.
        await turns(clock[0] + growbot_llm._AWAKE_BREAK_S - 61.0)
        assert marks() == held
        # After the break the hold starts again.
        await turns(clock[0] + 122.0)
        assert marks() > held

    asyncio.run(run())


def test_a_restore_whose_turn_failed_is_released_by_the_next_turn(tmp_path: Path) -> None:
    store = _Store()
    app, auth, _gw = _app(tmp_path, gateway=_Gateway(chunks=_cold(cached=7000), status=503))
    app.state.kv_prefix = store
    with TestClient(app) as client:
        client.post(_COMPLETIONS, json=_turn(), headers=auth)
        assert len(store.restores) == 1 and store.used == []
        app.state.growbot_llm_client_factory = _Gateway(chunks=_cold(cached=7000))
        assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
        assert client.post(_COMPLETIONS, json=_turn(), headers=auth).status_code == 200
    # One restore, released once, by the first turn that reached the slot.
    assert len(store.restores) == 1 and len(store.used) == 1


def test_a_failed_stamp_does_not_restart_the_awake_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jbrain.api import growbot_llm

    clock = [1000.0]
    monkeypatch.setattr(growbot_llm.time, "monotonic", lambda: clock[0])
    app, _auth, _gw = _app(tmp_path, gateway=_Gateway())

    class _Store2:
        async def mark_growbot_awake(self, _ctx: object) -> None:
            return None

    app.state.settings_store = _Store2()

    class _Req:
        pass

    req = _Req()
    req.app = app  # type: ignore[attr-defined]

    async def run() -> None:
        growbot_llm._mark_awake(req)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        since = app.state.growbot_awake_since
        clock[0] += 61.0
        app.state.growbot_awake_stamped = None  # what a failed write leaves
        growbot_llm._mark_awake(req)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert app.state.growbot_awake_since == since

    asyncio.run(run())
