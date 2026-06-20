"""ComfyUiImageGen against a mocked httpx transport — no live ComfyUI, no network
(rule #5 / DEVELOPMENT.md "no network in tests"). The route a real ComfyUI takes
is scripted by a handler: submit -> poll(history) -> view, plus the edit upload."""

import json

import httpx
import pytest

from jbrain.image_gen.comfyui import (
    ComfyUiImageGen,
    EditSpec,
    GenSpec,
    ImageGenError,
    ImageGenTimeout,
)
from jbrain.image_gen.fake import FakeImageGen

BASE = "http://comfyui:8188"
PNG = b"\x89PNG\r\n\x1a\n" + b"the-rendered-bytes"

GEN = GenSpec(prompt="a cat", width=768, height=512, steps=12, seed=42, model="qwen-image-2512")
EDIT = EditSpec(prompt="add a hat", width=512, height=512, steps=8, seed=7, model="qwen-image-edit")

_OUT_IMAGE = {"filename": "out.png", "subfolder": "", "type": "output"}
_HISTORY_DONE = {"abc123": {"outputs": {"7": {"images": [_OUT_IMAGE]}}}}


def _client(handler, **kwargs) -> ComfyUiImageGen:  # type: ignore[no-untyped-def]
    """A driver whose HTTP is the handler and whose poll sleep is a no-op, so the
    timeout path runs without real waits. A fake monotonic clock can be injected."""

    async def _no_sleep(_: float) -> None:
        return None

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ComfyUiImageGen(BASE, http, sleep=_no_sleep, **kwargs)


async def test_generate_submit_poll_view_happy_path() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "abc123"})
        if request.url.path == "/history/abc123":
            return httpx.Response(200, json=_HISTORY_DONE)
        if request.url.path == "/view":
            assert request.url.params["filename"] == "out.png"
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)

    out = await _client(handle).generate(GEN)
    assert out == PNG
    # No source upload on a text->image generation; the three stages ran in order.
    assert calls == ["POST /prompt", "GET /history/abc123", "GET /view"]


async def test_generate_fills_prompt_seed_steps_and_dims() -> None:
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            wf = json.loads(request.content)["prompt"]
            seen["wf"] = wf
            return httpx.Response(200, json={"prompt_id": "abc123"})
        if request.url.path == "/history/abc123":
            return httpx.Response(200, json=_HISTORY_DONE)
        return httpx.Response(200, content=PNG)

    await _client(handle).generate(GEN)
    wf = seen["wf"]
    assert wf["2"]["inputs"]["text"] == "a cat"
    assert wf["5"]["inputs"]["seed"] == 42
    assert wf["5"]["inputs"]["steps"] == 12
    assert wf["4"]["inputs"]["width"] == 768 and wf["4"]["inputs"]["height"] == 512


async def test_edit_uploads_source_then_renders() -> None:
    uploaded: list[bytes] = []
    referenced: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            uploaded.append(request.content)
            return httpx.Response(200, json={"name": "input.png", "subfolder": "", "type": "input"})
        if request.url.path == "/prompt":
            referenced["image"] = json.loads(request.content)["prompt"]["8"]["inputs"]["image"]
            return httpx.Response(200, json={"prompt_id": "abc123"})
        if request.url.path == "/history/abc123":
            return httpx.Response(200, json=_HISTORY_DONE)
        return httpx.Response(200, content=PNG)

    source = b"\x89PNG\r\n\x1a\nsource"
    out = await _client(handle).edit(EDIT, source)
    assert out == PNG
    # The source bytes were uploaded and the returned server name fed the graph.
    assert uploaded and source in uploaded[0]
    assert referenced["image"] == "input.png"


async def test_await_polls_until_outputs_appear() -> None:
    polls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "abc123"})
        if request.url.path == "/history/abc123":
            polls["n"] += 1
            # Queued (empty), then running (no outputs), then done.
            if polls["n"] == 1:
                return httpx.Response(200, json={})
            if polls["n"] == 2:
                return httpx.Response(200, json={"abc123": {"outputs": {}}})
            return httpx.Response(200, json=_HISTORY_DONE)
        return httpx.Response(200, content=PNG)

    out = await _client(handle).generate(GEN)
    assert out == PNG and polls["n"] == 3


async def test_timeout_when_history_never_completes() -> None:
    clock = {"t": 0.0}

    def tick() -> float:
        clock["t"] += 1.0  # each monotonic read advances a second; budget=2.5s
        return clock["t"]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "abc123"})
        # Always still queued — outputs never arrive.
        return httpx.Response(200, json={"abc123": {"outputs": {}}})

    gen = _client(handle, timeout=2.5, monotonic=tick)
    with pytest.raises(ImageGenTimeout):
        await gen.generate(GEN)


async def test_submit_error_payload_raises_image_gen_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        # ComfyUI rejects an invalid graph with node_errors and no prompt_id.
        return httpx.Response(200, json={"error": "bad node", "node_errors": {}})

    with pytest.raises(ImageGenError):
        await _client(handle).generate(GEN)


async def test_runtime_error_status_fails_fast_not_timeout() -> None:
    # A node that errors mid-run leaves status_str="error" + empty outputs; this
    # must raise ImageGenError immediately, not poll out the whole timeout budget.
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "abc123"})
        return httpx.Response(
            200,
            json={"abc123": {"status": {"status_str": "error"}, "outputs": {}}},
        )

    with pytest.raises(ImageGenError):
        await _client(handle).generate(GEN)


async def test_empty_view_body_raises_image_gen_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "abc123"})
        if request.url.path == "/history/abc123":
            return httpx.Response(200, json=_HISTORY_DONE)
        return httpx.Response(200, content=b"")

    with pytest.raises(ImageGenError):
        await _client(handle).generate(GEN)


async def test_http_error_raises_image_gen_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(ImageGenError):
        await _client(handle).generate(GEN)


async def test_fake_image_gen_returns_valid_png_and_records_specs() -> None:
    fake = FakeImageGen()
    gen_bytes = await fake.generate(GEN)
    assert gen_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert fake.last_gen == GEN

    source = b"\x89PNG\r\n\x1a\nsource"
    edit_bytes = await fake.edit(EDIT, source)
    assert edit_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert fake.last_edit == EDIT and fake.last_source == source
