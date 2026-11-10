"""The GrowBot creature's model lane (api/growbot_llm.py): `growbot.token` auth, the installed
models with their vision flag, and the residency-admitted, slot-pinned forward to the gateway.
A bare app with a fake residency and a faked gateway client: no network, no model.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import growbot_llm
from jbrain.llm import engine, gpu_guard, local_catalog, prefill
from jbrain.llm.kv_pool_guard import KvPoolGuard
from jbrain.llm.residency import ResidencyError
from jbrain.llm.slot_roles import FLASH_NEXT_POOL, GROWBOT_ROLE
from jbrain.storage import FsInternalSecrets

_MODELS = "/internal/growbot/llm/v1/models"
_COMPLETIONS = "/internal/growbot/llm/v1/chat/completions"
_VL = "qwen3-vl-30b-a3b"
_FLASH = "qwen3.8-flash-next"
_IMAGE = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
# What the adapter sends Qwen3.8's template for our "high": its own scale differs.
_QWEN38 = local_catalog.get("qwen3.8-27b")
assert _QWEN38 is not None
_QWEN38_HIGH = _QWEN38.thinking_effort_map["high"]


class _Residency:
    def __init__(self, admit: str | None = None, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._admit = admit
        self._error = error

    async def ensure_room(self, served: str) -> str | None:
        self.calls.append(served)
        if self._error is not None:
            raise self._error
        return self._admit or served


class _Gateway:
    """Records what the proxy sent; answers with `chunks`, or fails with `error`."""

    def __init__(
        self,
        chunks: tuple[bytes, ...] = (b"ok",),
        error: Exception | None = None,
        status: int = 200,
    ):
        self.status = status
        self.sent: list[dict[str, Any]] = []
        self.base_url = ""
        self.closed = 0
        self._chunks = chunks
        self._error = error

    def __call__(self, *_a: object, **kw: object) -> _Gateway:
        self.base_url = str(kw.get("base_url"))
        return self

    def stream(self, method: str, path: str, *, json: dict[str, Any]) -> Any:
        gateway = self

        class _Stream:
            status_code = gateway.status

            async def __aenter__(self) -> _Stream:
                if gateway._error is not None:
                    raise gateway._error
                gateway.sent.append({"method": method, "path": path, "json": json})
                return self

            async def __aexit__(self, *_a: object) -> None:
                return None

            async def aiter_raw(self) -> Any:
                for chunk in gateway._chunks:
                    yield chunk

        return _Stream()

    async def aclose(self) -> None:
        self.closed += 1


def _app(
    tmp_path: Path,
    *,
    models: tuple[str, ...] = ("qwen3-vl-30b", "gpt-oss-120b", "qwen3.8-27b"),
    enabled: bool = True,
    gateway_url: str = "http://gw:8080/v1",
    residency: object | None = None,
    gateway: _Gateway | None = None,
    active: str | None = None,
) -> tuple[FastAPI, dict[str, str], _Gateway]:
    app = FastAPI()
    app.include_router(growbot_llm.router, prefix="/internal")
    app.state.settings = SimpleNamespace(
        local_llm_enabled=enabled, local_models=list(models), local_llm_url=gateway_url
    )
    app.state.residency = residency
    gw = gateway or _Gateway()
    app.state.growbot_llm_client_factory = gw
    secrets = FsInternalSecrets(tmp_path)
    secrets.ensure("growbot")
    app.state.growbot_secrets = secrets
    if active is not None:

        async def _load() -> str:
            return active

        app.state.active_engine = engine.ActiveEngine(_load, ttl_s=0.0)
    token = (secrets.read("growbot") or b"").decode()
    return app, {"Authorization": f"Bearer {token}"}, gw


@pytest.fixture(autouse=True)
def _fresh_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefill, "_ratio", {})


def _chat(model: str = _VL, content: object = "hi", **extra: object) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": content}], **extra}


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Token x"}]
)
def test_every_route_needs_the_growbot_token(tmp_path: Path, headers: dict[str, str]) -> None:
    res = _Residency()
    app, _, gw = _app(tmp_path, residency=res)
    client = TestClient(app)
    assert client.get(_MODELS, headers=headers).status_code == 401
    assert client.post(_COMPLETIONS, json=_chat(), headers=headers).status_code == 401
    assert res.calls == [] and gw.sent == []


def test_models_list_the_one_creature_model_vision_first(tmp_path: Path) -> None:
    app, auth, _ = _app(tmp_path)
    data = TestClient(app).get(_MODELS, headers=auth).json()["data"]
    assert [(m["id"], m["vision"]) for m in data] == [(_VL, True)]
    assert data[0]["object"] == "model" and data[0]["label"]
    (tmp_path / "blind").mkdir()
    blind, auth, _ = _app(tmp_path / "blind", models=("gpt-oss-120b",))
    data = TestClient(blind).get(_MODELS, headers=auth).json()["data"]
    assert [(m["id"], m["vision"]) for m in data] == [("gpt-oss-120b", False)]


def test_models_follow_the_serving_engine_and_hosting(tmp_path: Path) -> None:
    app, auth, _ = _app(tmp_path, models=(_FLASH, "qwen3-vl-30b"), active="flash-next")
    data = TestClient(app).get(_MODELS, headers=auth).json()["data"]
    assert [(m["id"], m["vision"]) for m in data] == [(_FLASH, True)]
    (tmp_path / "off").mkdir()
    off, auth, _ = _app(tmp_path / "off", enabled=False)
    assert TestClient(off).get(_MODELS, headers=auth).json()["data"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_a_turn_is_admitted_then_forwarded_verbatim(tmp_path: Path, stream: bool) -> None:
    res = _Residency()
    gw = _Gateway(chunks=(b"data: {}\n\n", b"data: [DONE]\n\n"))
    app, auth, _ = _app(tmp_path, residency=res, gateway=gw)
    r = TestClient(app).post(
        _COMPLETIONS, json=_chat(content=[_IMAGE], stream=stream), headers=auth
    )
    assert r.status_code == 200
    assert r.text == "data: {}\n\ndata: [DONE]\n\n"
    media = "text/event-stream" if stream else "application/json"
    assert r.headers["content-type"].startswith(media)
    assert res.calls == [_VL]
    assert gw.base_url == "http://gw:8080/v1"
    (sent,) = gw.sent
    assert (sent["method"], sent["path"]) == ("POST", "/chat/completions")
    assert sent["json"]["model"] == _VL
    assert sent["json"]["messages"][0]["content"] == [_IMAGE]
    assert sent["json"]["max_tokens"] == growbot_llm.DEFAULT_MAX_TOKENS
    assert gw.closed == 1


def test_a_catalog_id_names_its_served_model(tmp_path: Path) -> None:
    app, auth, gw = _app(tmp_path, residency=_Residency())
    assert (
        TestClient(app).post(_COMPLETIONS, json=_chat("qwen3-vl-30b"), headers=auth).status_code
        == 200
    )
    assert gw.sent[0]["json"]["model"] == _VL


def test_the_page_never_chooses_what_loads(tmp_path: Path) -> None:
    # Upstream's lanes name cloud models, other installed models, or nothing: every one runs
    # on the creature's model, so no turn can make the box swap.
    res = _Residency()
    app, auth, gw = _app(tmp_path, residency=res)
    client = TestClient(app)
    for name in ("google/gemini-2.5-flash", "gpt-oss-120b", "qwen3.8-27b", "llama-3.3-70b", ""):
        assert client.post(_COMPLETIONS, json=_chat(name), headers=auth).status_code == 200
    assert res.calls == [_VL] * 5
    assert {s["json"]["model"] for s in gw.sent} == {_VL}


def test_no_installed_model_is_refused_before_admission(tmp_path: Path) -> None:
    res = _Residency()
    app, auth, gw = _app(tmp_path, residency=res, enabled=False)
    r = TestClient(app).post(_COMPLETIONS, json=_chat(), headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_found"
    assert res.calls == [] and gw.sent == []


def test_only_chat_fields_reach_the_engine(tmp_path: Path) -> None:
    app, auth, gw = _app(tmp_path)
    extra = {
        "grammar": "root ::= x",
        "json_schema": {},
        "n_probs": 50,
        "logit_bias": {"1": 5},
        "cache_prompt": False,
        "samplers": ["x"],
        "tools": [],
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
        "stop": ["\n"],
    }
    assert TestClient(app).post(_COMPLETIONS, json=_chat(**extra), headers=auth).status_code == 200
    sent = gw.sent[0]["json"]
    assert set(sent) == {"model", "messages", "temperature", "stop", "max_tokens"}


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        ({"max_tokens": 10_000_000}, growbot_llm.DEFAULT_MAX_TOKENS),
        ({"max_completion_tokens": 10_000_000}, growbot_llm.DEFAULT_MAX_TOKENS),
        ({"max_tokens": 40, "max_completion_tokens": 900}, 40),
        ({"max_tokens": 0}, growbot_llm.DEFAULT_MAX_TOKENS),
        ({"max_tokens": True}, growbot_llm.DEFAULT_MAX_TOKENS),
        ({"max_tokens": "50"}, growbot_llm.DEFAULT_MAX_TOKENS),
    ],
)
def test_the_output_budget_is_capped_on_an_engine_with_no_pool(
    tmp_path: Path, budget: dict[str, object], expected: int
) -> None:
    app, auth, gw = _app(tmp_path)
    assert TestClient(app).post(_COMPLETIONS, json=_chat(**budget), headers=auth).status_code == 200
    sent = gw.sent[0]["json"]
    assert sent["max_tokens"] == expected and "max_completion_tokens" not in sent


def test_an_oversized_inline_image_is_refused(tmp_path: Path) -> None:
    res = _Residency()
    app, auth, gw = _app(tmp_path, residency=res)
    url = "data:image/jpeg;base64," + "A" * growbot_llm.MAX_IMAGE_CHARS
    body = _chat(content=[{"type": "image_url", "image_url": {"url": url}}])
    r = TestClient(app).post(_COMPLETIONS, json=body, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_content"
    assert res.calls == [] and gw.sent == []


@pytest.mark.parametrize(
    "part",
    [
        # The engine would fetch these itself: SSRF from the box.
        {"type": "image_url", "image_url": {"url": "http://169.254.169.254/latest/meta-data"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/cat.jpg"}},
        {"type": "image_url", "image_url": "http://router.lan/admin"},
        {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}},
        {"type": "image_url", "image_url": {"url": "data:text/html;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/png,not-base64"}},
        {"type": "image_url", "image_url": {"url": " data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {}},
        {"type": "image_url"},
        # Other part kinds may carry URLs too: only text and inline images pass.
        {"type": "video_url", "video_url": {"url": "http://x/v.mp4"}},
        {"type": "file", "file": {"url": "http://x/f"}},
        "http://x/bare-string-part",
    ],
)
def test_a_non_inline_image_or_unknown_part_is_refused(tmp_path: Path, part: object) -> None:
    res = _Residency()
    app, auth, gw = _app(tmp_path, residency=res)
    body = _chat(content=[{"type": "text", "text": "look"}, part])
    r = TestClient(app).post(_COMPLETIONS, json=body, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_content"
    assert res.calls == [] and gw.sent == []


def test_an_inline_image_in_any_message_passes(tmp_path: Path) -> None:
    app, auth, gw = _app(tmp_path, residency=_Residency())
    png = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
    body = {
        "model": _VL,
        "messages": [
            {"role": "system", "content": "you are a robot"},
            {"role": "user", "content": [png, _IMAGE, {"type": "text", "text": "who is this"}]},
        ],
    }
    assert TestClient(app).post(_COMPLETIONS, json=body, headers=auth).status_code == 200
    assert len(gw.sent) == 1


def test_a_remote_image_in_an_earlier_turn_is_refused_too(tmp_path: Path) -> None:
    app, auth, gw = _app(tmp_path, residency=_Residency())
    remote = {"type": "image_url", "image_url": {"url": "http://10.0.0.1/"}}
    body = {
        "model": _VL,
        "messages": [
            {"role": "user", "content": [remote]},
            {"role": "assistant", "content": "a cat"},
            {"role": "user", "content": [_IMAGE]},
        ],
    }
    assert TestClient(app).post(_COMPLETIONS, json=body, headers=auth).status_code == 400
    assert gw.sent == []


def test_an_image_for_a_blind_model_is_refused(tmp_path: Path) -> None:
    res = _Residency()
    app, auth, gw = _app(tmp_path, models=("gpt-oss-120b",), residency=res)
    body = _chat("gpt-oss-120b", content=[{"type": "text", "text": "what is this"}, _IMAGE])
    r = TestClient(app).post(_COMPLETIONS, json=body, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "vision_not_supported"
    assert res.calls == [] and gw.sent == []


def test_while_flash_next_serves_a_standard_name_runs_on_it(tmp_path: Path) -> None:
    app, auth, gw = _app(
        tmp_path, models=(_FLASH, "qwen3-vl-30b"), residency=_Residency(), active="flash-next"
    )
    r = TestClient(app).post(_COMPLETIONS, json=_chat(_VL, content=[_IMAGE]), headers=auth)
    assert r.status_code == 200
    sent = gw.sent[0]["json"]
    assert sent["model"] == _FLASH
    # Pooled: held to the GrowBot slot and pinned there, never a slot the client names.
    assert sent["id_slot"] == FLASH_NEXT_POOL.slot(GROWBOT_ROLE)


def test_the_client_cannot_pick_a_slot(tmp_path: Path) -> None:
    app, auth, gw = _app(tmp_path, models=(_FLASH,), active="flash-next")
    body = _chat(_FLASH, id_slot=0, slot_id=0, n_predict=-1, max_tokens=50)
    assert TestClient(app).post(_COMPLETIONS, json=body, headers=auth).status_code == 200
    sent = gw.sent[0]["json"]
    assert sent["id_slot"] == FLASH_NEXT_POOL.slot(GROWBOT_ROLE)
    assert "slot_id" not in sent and "n_predict" not in sent and sent["max_tokens"] == 50


def test_a_turn_too_long_for_the_slot_is_refused_before_admission(tmp_path: Path) -> None:
    res = _Residency()
    app, auth, gw = _app(tmp_path, models=(_FLASH,), residency=res, active="flash-next")
    body = _chat(_FLASH, content=[_IMAGE] * 20)
    r = TestClient(app).post(_COMPLETIONS, json=body, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "context_length_exceeded"
    assert res.calls == [] and gw.sent == []


def test_a_live_guard_pins_the_growbot_slot(tmp_path: Path) -> None:
    async def idle(model: str) -> list[dict[str, object]]:
        return [
            {"id": i, "is_processing": False, "n_prompt_tokens": 0}
            for i in range(FLASH_NEXT_POOL.n_slots)
        ]

    async def erase(model: str, slot: int) -> bool:
        raise AssertionError("an empty pool needs no room made")

    app, auth, gw = _app(tmp_path, models=(_FLASH,), active="flash-next")
    app.state.kv_pool_guard = guard = KvPoolGuard(idle, erase)
    assert TestClient(app).post(_COMPLETIONS, json=_chat(_FLASH), headers=auth).status_code == 200
    assert gw.sent[0]["json"]["id_slot"] == FLASH_NEXT_POOL.slot(GROWBOT_ROLE)
    assert guard._pending == {}


@pytest.mark.parametrize("stream", [False, True])
def test_a_busy_pool_answers_with_an_openai_error(tmp_path: Path, stream: bool) -> None:
    async def full(model: str) -> list[dict[str, object]]:
        # Every slot decoding with the pool already past full: no room, and no wait.
        return [
            {
                "id": i,
                "is_processing": True,
                "n_prompt_tokens": 60_000,
                "next_token": [{"n_remain": -1, "n_decoded": 1}],
            }
            for i in range(FLASH_NEXT_POOL.n_slots)
        ]

    async def erase(model: str, slot: int) -> bool:
        raise AssertionError("busy slots are never erased")

    app, auth, gw = _app(tmp_path, models=(_FLASH,), active="flash-next")
    app.state.kv_pool_guard = KvPoolGuard(full, erase, wait_s=0.0)
    r = TestClient(app).post(_COMPLETIONS, json=_chat(_FLASH, stream=stream), headers=auth)
    assert r.status_code == 200 and gw.sent == []
    error = _error(r, stream)
    assert error["code"] == "kv_pool_busy"


def _error(r: httpx.Response, stream: bool) -> dict[str, Any]:
    if stream:
        frames = [f for f in r.text.split("\n\n") if f]
        assert frames[-1] == "data: [DONE]"
        return json.loads(frames[0].removeprefix("data: "))["error"]
    return r.json()["error"]


@pytest.mark.parametrize(
    "refusal", [ResidencyError("too big"), gpu_guard.GpuBudgetError("no device room")]
)
@pytest.mark.parametrize("stream", [False, True])
def test_a_residency_refusal_ends_the_turn_before_the_gateway(
    tmp_path: Path, refusal: Exception, stream: bool
) -> None:
    app, auth, gw = _app(tmp_path, residency=_Residency(error=refusal))
    r = TestClient(app).post(_COMPLETIONS, json=_chat(stream=stream), headers=auth)
    assert r.status_code == 200
    assert _error(r, stream)["code"] == "model_unavailable"
    assert gw.sent == []  # the gateway never loads an unadmitted model
    assert gw.closed == 1


def test_a_housekeeping_hiccup_still_serves(tmp_path: Path) -> None:
    app, auth, gw = _app(tmp_path, residency=_Residency(error=RuntimeError("blip")))
    assert TestClient(app).post(_COMPLETIONS, json=_chat(), headers=auth).text == "ok"
    assert len(gw.sent) == 1


def test_the_admitted_model_is_the_one_sent(tmp_path: Path) -> None:
    app, auth, gw = _app(tmp_path, models=(_FLASH, "qwen3-vl-30b"), residency=_Residency(_FLASH))
    assert TestClient(app).post(_COMPLETIONS, json=_chat(), headers=auth).status_code == 200
    sent = gw.sent[0]["json"]
    assert sent["model"] == _FLASH
    assert sent["id_slot"] == FLASH_NEXT_POOL.slot(GROWBOT_ROLE)


@pytest.mark.parametrize("stream", [False, True])
def test_an_unreachable_gateway_is_an_openai_error(tmp_path: Path, stream: bool) -> None:
    gw = _Gateway(error=httpx.ConnectError("refused"))
    app, auth, _ = _app(tmp_path, gateway=gw)
    r = TestClient(app).post(_COMPLETIONS, json=_chat(stream=stream), headers=auth)
    assert _error(r, stream)["code"] == "gateway_unavailable"
    assert gw.closed == 1


def test_bad_bodies_and_no_engine(tmp_path: Path) -> None:
    app, auth, _ = _app(tmp_path)
    client = TestClient(app)
    assert client.post(_COMPLETIONS, content=b"{nope", headers=auth).status_code == 400
    assert client.post(_COMPLETIONS, json=[1], headers=auth).status_code == 400
    assert client.post(_COMPLETIONS, json={"model": _VL}, headers=auth).status_code == 400
    (tmp_path / "x").mkdir()
    bare, auth, _ = _app(tmp_path / "x", gateway_url="")
    assert TestClient(bare).post(_COMPLETIONS, json=_chat(), headers=auth).status_code == 503


@pytest.mark.parametrize(
    ("model", "extra", "expected"),
    [
        # A hybrid reasoner: thinking off unless the app asks, and OpenRouter's object dropped.
        ("qwen3.8-27b", {}, {"chat_template_kwargs": {"enable_thinking": False}}),
        (
            "qwen3.8-27b",
            {"reasoning": {"effort": "high"}},
            {"chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": _QWEN38_HIGH}},
        ),
        # gpt-oss has no real "off": its floor is low.
        ("gpt-oss-120b", {"reasoning_effort": "bogus"}, {"reasoning_effort": "low"}),
        ("gpt-oss-120b", {"reasoning_effort": "medium"}, {"reasoning_effort": "medium"}),
        # A model with no reasoning carries no field at all.
        (_VL, {"reasoning_effort": "high", "chat_template_kwargs": {"x": 1}}, {}),
    ],
)
def test_the_box_sets_the_reasoning_level(
    tmp_path: Path, model: str, extra: dict[str, Any], expected: dict[str, Any]
) -> None:
    installed = local_catalog.get_by_served(model)
    assert installed is not None
    app, auth, gw = _app(tmp_path, models=(installed.id,))
    assert (
        TestClient(app).post(_COMPLETIONS, json=_chat(model, **extra), headers=auth).status_code
        == 200
    )
    sent = gw.sent[0]["json"]
    got = {
        k: sent[k] for k in ("reasoning_effort", "reasoning", "chat_template_kwargs") if k in sent
    }
    assert got == expected


def test_a_turn_too_long_for_the_slot_it_was_admitted_to_is_an_openai_error(
    tmp_path: Path,
) -> None:
    # Fitted against an unpooled model, then admitted onto Flash-Next: re-fitted there.
    res = _Residency(_FLASH)
    app, auth, gw = _app(tmp_path, models=(_FLASH, "qwen3-vl-30b"), residency=res)
    body = _chat(content=[_IMAGE] * 20, stream=True)
    r = TestClient(app).post(_COMPLETIONS, json=body, headers=auth)
    assert _error(r, True)["code"] == "context_length_exceeded"
    assert res.calls == [_VL] and gw.sent == []


class _Lock:
    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self) -> None:
        self.entered += 1

    async def __aexit__(self, *_a: object) -> None:
        return None


@pytest.mark.parametrize("pooled", [False, True])
def test_a_turn_that_may_swap_takes_the_box_wide_swap_lock(tmp_path: Path, pooled: bool) -> None:
    models, active = ((_FLASH,), "flash-next") if pooled else (("qwen3-vl-30b",), None)
    app, auth, _ = _app(tmp_path, models=models, active=active)
    swap, own = _Lock(), _Lock()
    app.state.jcode_llm_swap_lock = swap
    app.state.growbot_llm_lock = own
    assert TestClient(app).post(_COMPLETIONS, json=_chat(), headers=auth).status_code == 200
    # Standard swaps under jcode's lock; a pooled engine serves one model and keeps its own.
    assert (swap.entered, own.entered) == ((0, 1) if pooled else (1, 0))


def test_the_level_is_judged_on_the_admitted_model(tmp_path: Path) -> None:
    # The creature's model has no reasoning; admission switched the engine to a model that does.
    app, auth, gw = _app(tmp_path, residency=_Residency("qwen3.8-27b"))
    assert TestClient(app).post(_COMPLETIONS, json=_chat(), headers=auth).status_code == 200
    sent = gw.sent[0]["json"]
    assert sent["model"] == "qwen3.8-27b"
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("stream", [False, True])
def test_a_gateway_error_status_is_an_openai_error(tmp_path: Path, stream: bool) -> None:
    gw = _Gateway(chunks=(b"<html>boom</html>",), status=500)
    app, auth, _ = _app(tmp_path, gateway=gw)
    r = TestClient(app).post(_COMPLETIONS, json=_chat(stream=stream), headers=auth)
    assert r.status_code == 200
    assert "boom" not in r.text
    assert _error(r, stream)["code"] == "gateway_error"
