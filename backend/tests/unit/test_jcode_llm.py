"""The residency-aware jcode LLM proxy: shared-token auth, the installed-model list, and
the evict-then-forward completion path that makes a live grok `/model` switch a safe cold
swap. Runs the router on a bare app with a fake residency + a faked gateway (no network).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jcode_llm
from jbrain.llm import engine, local_catalog, openai_slot_fit, prefill, slot_roles
from jbrain.llm.engine_effort import EngineEfforts
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
    # Too long for the slot it was remapped to: too late for a 400, so nothing is forwarded
    # and the 200's body carries OpenAI's overflow error instead of being empty.
    sent.clear()
    long = {"model": "gpt-oss-120b", "messages": [{"role": "user", "content": "x" * 1_200_000}]}
    r = client.post(_COMPLETIONS, json=long, headers=_AUTH)
    assert r.json()["error"]["code"] == "context_length_exceeded" and "payload" not in sent


def _busy_guard() -> KvPoolGuard:
    # Every other slot busy decoding near its cap: no room for a jcode call, and no wait.
    async def full(model: str) -> list[dict[str, object]]:
        return [
            {
                "id": i,
                "is_processing": i != 4,
                "n_prompt_tokens": 0 if i == 4 else 200_000,
                "next_token": [{"n_remain": -1, "n_decoded": 1}],
            }
            for i in range(8)
        ]

    async def erase(model: str, slot: int) -> bool:
        raise AssertionError("busy slots are never erased")

    return KvPoolGuard(full, erase, wait_s=0.0)


@pytest.mark.usefixtures("_fresh_ratio")
@pytest.mark.parametrize("stream", [True, False])
def test_a_busy_pool_answers_with_an_openai_error_not_a_cut_stream(
    monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    app = _flash_app()
    app.state.kv_pool_guard = _busy_guard()
    body = {"messages": [], "max_tokens": 1000, "stream": stream}
    r, payload = _post(app, monkeypatch, body)
    assert r.status_code == 200 and payload == {}
    if stream:
        frames = [f for f in r.text.split("\n\n") if f]
        assert frames[-1] == "data: [DONE]"
        error = json.loads(frames[0].removeprefix("data: "))["error"]
    else:
        error = r.json()["error"]
    assert error["code"] == "kv_pool_busy" and error["type"] == "server_error"


@pytest.mark.usefixtures("_fresh_ratio")
def test_parallel_choices_are_dropped_and_odd_budgets_read_as_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"messages": [], "n": 4, "n_cmpl": 4, "max_tokens": "800", "max_completion_tokens": None}
    r, payload = _post(_flash_app(), monkeypatch, body)
    assert r.status_code == 200
    assert "n" not in payload and "n_cmpl" not in payload
    # The fitted budget under both names: the client's null is never forwarded.
    assert payload["max_tokens"] == payload["max_completion_tokens"] == 800


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


def test_the_openai_estimate_counts_text_the_way_the_router_does() -> None:
    # Content text, tool calls and tool schemas count; JSON punctuation and base64 image data
    # do not (the image is charged flat instead).
    schema = {"type": "object"}
    payload = {
        "messages": [
            {"role": "system", "content": "s" * 100},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "t" * 50},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 9}},
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "search", "arguments": '{"q": "x"}'}}],
            },
            {"role": "tool", "tool_call_id": "1", "content": "r" * 30},
        ],
        "tools": [{"type": "function", "function": {"name": "search", "parameters": schema}}],
    }
    chars, images = openai_slot_fit.prompt_chars(payload)
    assert images == 1
    assert chars == (
        100
        + 50
        + slot_roles.tool_call_chars("search", {"q": "x"})
        + 30
        + slot_roles.tool_schema_chars("search", "", schema)
    )


# --- the owner's Flash-Next level per code-mode role ------------------------------------------


class _Store:
    """The two code-mode picks the proxy reads to tell a planner request from an executor."""

    def __init__(self, executor: str = "qwen3-coder-next", planner: str = "gpt-oss-120b") -> None:
        self.executor, self.planner = executor, planner

    async def jcode_model(self, ctx: object) -> str:
        return self.executor

    async def jcode_planner_model(self, ctx: object) -> str:
        return self.planner


class _Efforts:
    def __init__(self, rows: dict[tuple[str, str, str], str]) -> None:
        self.value = EngineEfforts(rows)

    async def get(self) -> EngineEfforts:
        return self.value


_LEVELS = {
    ("flash-next", "task", "jcode.executor"): "low",
    ("flash-next", "task", "jcode.planner"): "high",
}


def _coder_app(
    rows: dict[tuple[str, str, str], str], *, store: _Store | None = None, flash: bool = True
) -> FastAPI:
    app = _app(models=(_FLASH, "gpt-oss-120b", "qwen3-coder-next"))
    if flash:

        async def _load() -> str:
            return "flash-next"

        app.state.active_engine = engine.ActiveEngine(_load, ttl_s=0.0)
    app.state.settings_store = store or _Store()
    app.state.engine_efforts = _Efforts(rows)
    return app


def _send(app: FastAPI, monkeypatch: pytest.MonkeyPatch, body: dict) -> dict:
    sent: dict[str, object] = {}
    _fake_gateway(monkeypatch, sent, chunks=(b"ok",))
    r = TestClient(app).post(_COMPLETIONS, json=body, headers=_AUTH)
    assert r.status_code == 200
    return cast("dict", sent["payload"])


@pytest.mark.usefixtures("_fresh_ratio")
@pytest.mark.parametrize(
    ("requested", "level"),
    [
        # grok's default block is the executor; its `plan` pin is the planner. Both run on
        # Flash-Next, each at its own level (our `high` is Qwen3.8's template `xhigh`).
        ("qwen3-coder-next", "low"),
        ("gpt-oss-120b", "xhigh"),
    ],
)
def test_each_role_runs_on_flash_next_at_its_own_level(
    monkeypatch: pytest.MonkeyPatch, requested: str, level: str
) -> None:
    payload = _send(_coder_app(_LEVELS), monkeypatch, {"model": requested, "messages": []})
    assert payload["model"] == _FLASH
    assert payload["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": level}


@pytest.mark.usefixtures("_fresh_ratio")
def test_a_role_without_a_row_takes_the_code_tier_and_none_turns_thinking_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        ("flash-next", "tier", "code"): "none",
        ("flash-next", "task", "jcode.executor"): "low",
    }
    payload = _send(_coder_app(rows), monkeypatch, {"model": "gpt-oss-120b", "messages": []})
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.usefixtures("_fresh_ratio")
def test_the_owners_level_replaces_every_reasoning_field_grok_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "model": "qwen3-coder-next",
        "messages": [],
        "reasoning_effort": "high",
        "reasoning": {"effort": "high"},
        "chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "xhigh", "x": 1},
    }
    payload = _send(_coder_app(_LEVELS), monkeypatch, body)
    assert "reasoning_effort" not in payload and "reasoning" not in payload
    # Unrelated template kwargs survive; the reasoning ones are the owner's.
    assert payload["chat_template_kwargs"] == {
        "x": 1,
        "enable_thinking": True,
        "reasoning_effort": "low",
    }


@pytest.mark.usefixtures("_fresh_ratio")
def test_with_no_row_grok_s_own_level_goes_through_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"model": "qwen3-coder-next", "messages": [], "reasoning_effort": "medium"}
    payload = _send(_coder_app({}), monkeypatch, body)
    assert payload["reasoning_effort"] == "medium" and "chat_template_kwargs" not in payload


@pytest.mark.usefixtures("_fresh_ratio")
@pytest.mark.parametrize(
    "store",
    [
        _Store(planner="same"),  # single-model: the plan subagent is the executor
        _Store(executor="gpt-oss-120b", planner="gpt-oss-120b"),  # one model in both picks
    ],
)
def test_single_model_code_mode_is_all_executor(
    monkeypatch: pytest.MonkeyPatch, store: _Store
) -> None:
    payload = _send(
        _coder_app(_LEVELS, store=store), monkeypatch, {"model": "gpt-oss-120b", "messages": []}
    )
    assert payload["chat_template_kwargs"]["reasoning_effort"] == "low"


@pytest.mark.usefixtures("_fresh_ratio")
def test_a_failed_level_read_forwards_groks_own_level(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Broken:
        async def get(self) -> EngineEfforts:
            raise RuntimeError("db down")

    app = _coder_app(_LEVELS)
    app.state.engine_efforts = _Broken()
    body = {"model": "qwen3-coder-next", "messages": [], "reasoning_effort": "medium"}
    payload = _send(app, monkeypatch, body)
    assert payload["reasoning_effort"] == "medium" and "chat_template_kwargs" not in payload


def test_on_standard_the_body_is_forwarded_byte_for_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {**_LEVELS, ("flash-next", "tier", "code"): "none"}
    body = {"model": "gpt-oss-120b", "messages": [], "reasoning_effort": "high"}
    payload = _send(_coder_app(rows, flash=False), monkeypatch, body)
    assert payload == body


def test_under_flash_next_the_config_keeps_a_block_for_each_standard_coder() -> None:
    # A shell opened while Flash-Next serves still renders blocks for the session's executor
    # and planner names, so grok's default resolves and its `plan` pin stays a separate name.
    lines = TestClient(_coder_app({})).get(f"{_MODELS}?format=lines", headers=_AUTH).text
    rows = [ln.split("|") for ln in lines.strip().splitlines()]
    assert [r[1] for r in rows] == [_FLASH, "gpt-oss-120b", "qwen3-coder-next"]
    assert [r[0] for r in rows[1:]] == ["oss", "qwen"]
    flash_label = rows[0][2]
    assert all(r[2] == flash_label and r[3] == str(_JCODE_CAP) for r in rows)
    # The JSON list is still the engine's own models only.
    data = TestClient(_coder_app({})).get(_MODELS, headers=_AUTH).json()["data"]
    assert [m["id"] for m in data] == [_FLASH]
    # On Standard no extra handle is added.
    std = TestClient(_coder_app({}, flash=False)).get(f"{_MODELS}?format=lines", headers=_AUTH)
    assert _FLASH not in std.text and std.text.count("\n") == 2
