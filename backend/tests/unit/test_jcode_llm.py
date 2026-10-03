"""The residency-aware jcode LLM proxy: shared-token auth, the installed-model list, and
the evict-then-forward completion path that makes a live grok `/model` switch a safe cold
swap. Runs the router on a bare app with a fake residency + a faked gateway (no network).
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jcode_llm
from jbrain.llm import engine, local_catalog, prefill, slot_roles
from jbrain.llm.kv_pool_guard import KvPoolGuard
from jbrain.llm.openai_slot_fit import DEFAULT_OUTPUT_TOKENS
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, JCODE_ROLE

_AUTH = {"Authorization": "Bearer sk-test"}


class _RecordingResidency:
    """Captures the served names ensure_room was asked to make room for, in order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ensure_room(self, served: str) -> None:
        self.calls.append(served)


def _app(
    *,
    token: str = "sk-test",
    enabled: bool = True,
    models: tuple[str, ...] = ("gpt-oss-120b", "qwen3-coder-next"),
    gateway: str = "http://gw:8080/v1",
    residency: object | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(jcode_llm.router, prefix="/api")
    app.state.settings = SimpleNamespace(
        jcode_gateway_token=token,
        local_llm_enabled=enabled,
        local_models=list(models),
        local_llm_url=gateway,
    )
    app.state.residency = residency
    return app


def _fake_gateway(monkeypatch: pytest.MonkeyPatch, sent: dict, chunks: tuple[bytes, ...]) -> None:
    class _Stream:
        def __init__(self, payload: object) -> None:
            sent["payload"] = payload

        async def __aenter__(self) -> _Stream:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def aiter_raw(self):  # noqa: ANN202
            for c in chunks:
                yield c

    class _Client:
        def __init__(self, *a: object, **k: object) -> None:
            sent["base_url"] = k.get("base_url")

        def stream(self, method: str, path: str, *, json: object, **_k: object):  # noqa: ANN202
            sent["method"], sent["path"] = method, path
            return _Stream(json)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(jcode_llm.httpx, "AsyncClient", _Client)


_MODELS = "/api/jcode/llm/v1/models"
_COMPLETIONS = "/api/jcode/llm/v1/chat/completions"


def test_requires_the_shared_token() -> None:
    client = TestClient(_app())
    assert client.get(_MODELS).status_code == 401
    assert client.get(_MODELS, headers={"Authorization": "Bearer no"}).status_code == 401
    body = {"model": "qwen3-coder-next", "messages": []}
    assert client.post(_COMPLETIONS, json=body).status_code == 401


def test_empty_configured_token_fails_closed() -> None:
    client = TestClient(_app(token=""))
    # No token configured (code mode unprovisioned) → even a matching-looking bearer is refused.
    assert client.get(_MODELS, headers={"Authorization": "Bearer "}).status_code == 401


def test_models_lists_installed_tool_capable() -> None:
    client = TestClient(_app(models=("gpt-oss-120b", "qwen3-coder-next")))
    data = client.get(_MODELS, headers=_AUTH).json()
    # JSON `id` is the real served name (the API model id).
    assert {m["id"] for m in data["data"]} == {"gpt-oss-120b", "qwen3-coder-next"}
    # The shell-friendly form: alias|served|label|window per line, one config.toml block each.
    lines = client.get(f"{_MODELS}?format=lines", headers=_AUTH).text.strip().splitlines()
    by_served = {}
    for ln in lines:
        alias, served, label, window = ln.split("|")
        assert alias and served and label and window.isdigit()
        by_served[served] = alias
    # Short `/model` handles map onto the real served names.
    assert by_served == {"gpt-oss-120b": "oss", "qwen3-coder-next": "qwen"}


def test_models_empty_when_hosting_off() -> None:
    client = TestClient(_app(enabled=False))
    assert client.get(_MODELS, headers=_AUTH).json()["data"] == []


def test_completions_reject_a_model_outside_the_installed_set() -> None:
    res = _RecordingResidency()
    client = TestClient(_app(residency=res))
    r = client.post(
        _COMPLETIONS,
        json={"model": "some-huge-uninstalled-model", "messages": []},
        headers=_AUTH,
    )
    assert r.status_code == 400
    assert res.calls == []  # a bad name never drives an eviction


def test_completions_make_room_for_the_chosen_model_then_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    res = _RecordingResidency()
    app = _app(gateway="http://gw:8080/v1", residency=res)
    sent: dict[str, object] = {}
    _fake_gateway(monkeypatch, sent, chunks=(b'{"choices":[],"usage":{}}',))
    client = TestClient(app)

    body = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "plan"}]}
    r = client.post(_COMPLETIONS, json=body, headers=_AUTH)
    assert r.status_code == 200
    assert r.content == b'{"choices":[],"usage":{}}'
    # Room was made for the CALLER's model (a switch cold-swaps), not pinned to the coder.
    assert res.calls == ["gpt-oss-120b"]
    # Forwarded verbatim to the gateway's OpenAI endpoint; the model choice is honoured.
    assert sent["base_url"] == "http://gw:8080/v1"
    assert sent["path"] == "/chat/completions"
    assert sent["payload"]["model"] == "gpt-oss-120b"  # type: ignore[index]


def test_completions_survive_a_residency_hiccup(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        async def ensure_room(self, _served: str) -> None:
            raise RuntimeError("gateway probe failed")

    app = _app(residency=_Boom())
    sent: dict[str, object] = {}
    _fake_gateway(monkeypatch, sent, chunks=(b"ok",))
    client = TestClient(app)
    r = client.post(
        _COMPLETIONS,
        json={"model": "qwen3-coder-next", "messages": []},
        headers=_AUTH,
    )
    # Housekeeping failure degrades to the gateway's own load — the completion still forwards.
    assert r.status_code == 200 and r.content == b"ok"


@pytest.mark.asyncio
async def test_concurrent_different_model_requests_serialize() -> None:
    # Two overlapping requests for DIFFERENT models must not have their load/serve windows
    # interleave — the swap lock makes the second wait, so only one model is ever active.
    app = _app()
    app.state.jcode_llm_swap_lock = asyncio.Lock()
    events: list[str] = []

    class _Residency:
        async def ensure_room(self, served: str) -> None:
            events.append(f"room:{served}")

    app.state.residency = _Residency()

    class _Stream:
        def __init__(self, model: str) -> None:
            self.model = model

        async def __aenter__(self) -> _Stream:
            events.append(f"start:{self.model}")
            return self

        async def __aexit__(self, *a: object) -> None:
            events.append(f"end:{self.model}")

        async def aiter_raw(self):  # noqa: ANN202
            await asyncio.sleep(0.02)  # a real yield, so an unlocked pair WOULD interleave
            yield b"x"

    class _Client:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def stream(self, _m: str, _p: str, *, json: dict, **_k: object):  # noqa: ANN202
            return _Stream(json["model"])

        async def aclose(self) -> None:
            return None

    app.state.jcode_llm_client_factory = _Client
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r1, r2 = await asyncio.gather(
            c.post(_COMPLETIONS, json={"model": "gpt-oss-120b", "messages": []}, headers=_AUTH),
            c.post(_COMPLETIONS, json={"model": "qwen3-coder-next", "messages": []}, headers=_AUTH),
        )
    assert r1.status_code == 200 and r2.status_code == 200

    # Each model's start…end window is contiguous — no other model started inside it.
    def contiguous(model: str) -> bool:
        s, e = events.index(f"start:{model}"), events.index(f"end:{model}")
        return not any(x.startswith("start:") for x in events[s + 1 : e])

    assert contiguous("gpt-oss-120b") and contiguous("qwen3-coder-next")


# --- the pooled engine: jcode's slot, its cap, and the window grok is told -----------------

_FLASH = "qwen3.8-flash-next"
_JCODE_CAP = FLASH_NEXT_POOL.cap(JCODE_ROLE)


@pytest.fixture
def _fresh_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefill, "_ratio", {})


def _flash_app(*, residency: object | None = None) -> FastAPI:
    app = _app(models=(_FLASH, "gpt-oss-120b"), residency=residency)

    async def _load() -> str:
        return "flash-next"

    app.state.active_engine = engine.ActiveEngine(_load, ttl_s=0.0)
    return app


def _post(app: FastAPI, monkeypatch: pytest.MonkeyPatch, body: dict) -> tuple[httpx.Response, dict]:
    sent: dict[str, object] = {}
    _fake_gateway(monkeypatch, sent, chunks=(b"ok",))
    r = TestClient(app).post(_COMPLETIONS, json={"model": _FLASH, **body}, headers=_AUTH)
    return r, cast("dict", sent.get("payload", {}))


@pytest.mark.usefixtures("_fresh_ratio")
def test_a_pooled_request_is_pinned_to_the_jcode_slot_with_its_budget_filled_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    r, payload = _post(_flash_app(), monkeypatch, {"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert payload["id_slot"] == FLASH_NEXT_POOL.slot(JCODE_ROLE)
    # Without a budget llama-server would generate until the slot hits the trained context;
    # the default is a coding turn's worth, since the guard charges all of it to the pool.
    assert payload["max_tokens"] == DEFAULT_OUTPUT_TOKENS


@pytest.mark.usefixtures("_fresh_ratio")
def test_the_client_cannot_choose_a_slot_or_bypass_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"messages": [], "id_slot": 0, "slot_id": 0, "n_predict": -1, "max_tokens": 500}
    r, payload = _post(_flash_app(), monkeypatch, body)
    assert r.status_code == 200
    assert payload["id_slot"] == FLASH_NEXT_POOL.slot(JCODE_ROLE)
    assert "slot_id" not in payload and "n_predict" not in payload
    assert payload["max_tokens"] == 500


@pytest.mark.usefixtures("_fresh_ratio")
def test_a_live_guard_on_the_pool_layout_pins_the_jcode_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def eight_slots(model: str) -> list[dict[str, object]]:
        return [{"id": i, "is_processing": False, "n_prompt_tokens": 0} for i in range(8)]

    async def erase(model: str, slot: int) -> bool:
        raise AssertionError("an empty pool needs no room made")

    app = _flash_app()
    app.state.kv_pool_guard = guard = KvPoolGuard(eight_slots, erase)
    r, payload = _post(app, monkeypatch, {"messages": [], "max_tokens": 1000})
    assert r.status_code == 200
    assert payload["id_slot"] == FLASH_NEXT_POOL.slot(JCODE_ROLE)
    assert guard._pending == {}  # released once the stream ended


class _RemapsToFlash:
    async def ensure_room(self, served: str) -> str:
        return _FLASH


@pytest.mark.usefixtures("_fresh_ratio")
def test_a_remap_after_admission_is_fitted_to_the_model_actually_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The engine switched between the proxy's read and residency's: gpt-oss was asked for,
    # Flash-Next admitted. The request is held to the jcode slot of the model it goes to.
    sent: dict[str, object] = {}
    _fake_gateway(monkeypatch, sent, chunks=(b"ok",))
    client = TestClient(_app(residency=_RemapsToFlash()))
    body = {"model": "gpt-oss-120b", "messages": [], "n_predict": -1}
    assert client.post(_COMPLETIONS, json=body, headers=_AUTH).status_code == 200
    payload = cast("dict", sent["payload"])
    assert payload["model"] == _FLASH and "n_predict" not in payload
    assert payload["id_slot"] == FLASH_NEXT_POOL.slot(JCODE_ROLE)
    assert payload["max_tokens"] == DEFAULT_OUTPUT_TOKENS
    # Too long for the slot it was remapped to: too late for a 400, so nothing is forwarded.
    sent.clear()
    long = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x" * 1_200_000}]}
    r = client.post(_COMPLETIONS, json=long, headers=_AUTH)
    assert r.content == b"" and "payload" not in sent


@pytest.mark.usefixtures("_fresh_ratio")
def test_an_output_budget_past_the_cap_is_clamped_under_either_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = "x" * int(200_000 * 3.7)
    body = {"messages": [{"role": "user", "content": prompt}], "max_completion_tokens": 100_000}
    r, payload = _post(_flash_app(), monkeypatch, body)
    assert r.status_code == 200
    assert payload["max_tokens"] == payload["max_completion_tokens"]
    assert 50_000 < payload["max_tokens"] < 100_000


@pytest.mark.usefixtures("_fresh_ratio")
def test_a_request_too_long_for_the_slot_is_refused_in_openais_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    res = _RecordingResidency()
    prompt = "x" * int(300_000 * 3.7)
    r, payload = _post(
        _flash_app(residency=res), monkeypatch, {"messages": [{"role": "user", "content": prompt}]}
    )
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "context_length_exceeded"
    assert "jcode" in error["message"] and prompt[:50] not in error["message"]
    # Refused before the swap lock: nothing was admitted or forwarded.
    assert res.calls == [] and payload == {}


@pytest.mark.usefixtures("_fresh_ratio")
def test_a_stale_slot_layout_sends_the_request_unpinned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def four_slots(model: str) -> list[dict[str, object]]:
        return [{"id": i, "is_processing": False} for i in range(4)]

    async def erase(model: str, slot: int) -> bool:
        raise AssertionError("nothing to erase")

    app = _flash_app()
    app.state.kv_pool_guard = KvPoolGuard(four_slots, erase)
    r, payload = _post(app, monkeypatch, {"messages": [], "max_tokens": 1000})
    assert r.status_code == 200
    assert "id_slot" not in payload and payload["max_tokens"] == 1000


def test_a_standard_engine_request_is_neither_pinned_nor_budgeted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: dict[str, object] = {}
    _fake_gateway(monkeypatch, sent, chunks=(b"ok",))
    body = {"model": "gpt-oss-120b", "messages": []}
    assert TestClient(_app()).post(_COMPLETIONS, json=body, headers=_AUTH).status_code == 200
    assert sent["payload"] == {"model": "gpt-oss-120b", "messages": []}


def test_grok_is_told_the_jcode_slots_cap_as_a_pooled_models_window() -> None:
    model = local_catalog.get_by_served(_FLASH)
    assert model is not None
    small = slot_roles.KvPool(
        262_144,
        tuple(
            dataclasses.replace(r, cap_tokens=100_000) if r.role is JCODE_ROLE else r
            for r in FLASH_NEXT_POOL.reservations
        ),
    )
    assert jcode_llm._window(dataclasses.replace(model, kv_pool=small)) == 100_000
    assert jcode_llm._window(dataclasses.replace(model, kv_pool=None)) == model.context_window
    lines = TestClient(_flash_app()).get(f"{_MODELS}?format=lines", headers=_AUTH).text
    assert f"|{_FLASH}|" in lines and lines.strip().endswith(f"|{_JCODE_CAP}")
