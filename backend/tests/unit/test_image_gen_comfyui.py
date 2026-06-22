"""ComfyUiImageGen against a mocked httpx transport — no live ComfyUI, no network
(rule #5 / DEVELOPMENT.md "no network in tests"). The route a real ComfyUI takes
is scripted by a handler: submit -> poll(history) -> view, plus the edit upload."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from websockets.asyncio.server import serve

from jbrain.image_gen.comfyui import (
    ComfyUiImageGen,
    EditSpec,
    GenSpec,
    ImageGenError,
    ImageGenInterrupted,
    ImageGenTimeout,
    _parse_preview,
)
from jbrain.image_gen.fake import FakeImageGen

BASE = "http://comfyui:8188"
PNG = b"\x89PNG\r\n\x1a\n" + b"the-rendered-bytes"

GEN = GenSpec(prompt="a cat", width=768, height=512, steps=12, seed=42, model="qwen-image-2512")
EDIT = EditSpec(
    prompt="add a hat",
    width=512,
    height=512,
    steps=8,
    seed=7,
    model="qwen-image-edit",
    megapixels=1.6,
)

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
    # Node ids match the on-box-validated Qwen-Image graph (see comfyui.py bindings).
    assert wf["6"]["inputs"]["text"] == "a cat"
    assert wf["3"]["inputs"]["seed"] == 42
    assert wf["3"]["inputs"]["steps"] == 12
    assert wf["58"]["inputs"]["width"] == 768 and wf["58"]["inputs"]["height"] == 512
    # No negative prompt given → the negative node keeps its authored (empty) default.
    assert wf["7"]["inputs"]["text"] == ""


async def test_generate_fast_model_drives_the_dreamshaper_sdxl_graph() -> None:
    """speed: fast routes to DreamShaper XL — the stock SDXL graph (CheckpointLoaderSimple),
    with prompt/seed/steps/dims filled into its node ids (6/3/5) and the negative on node 7."""
    import dataclasses

    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/prompt":
            seen["wf"] = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "abc123"})
        if request.url.path == "/history/abc123":
            return httpx.Response(200, json=_HISTORY_DONE)
        return httpx.Response(200, content=PNG)

    fast = dataclasses.replace(
        GEN, model="dreamshaper-xl-lightning", steps=4, negative_prompt="blurry"
    )
    await _client(handle).generate(fast)
    wf = seen["wf"]
    assert wf["4"]["class_type"] == "CheckpointLoaderSimple"  # the SDXL all-in-one loader
    assert wf["6"]["inputs"]["text"] == "a cat"
    assert wf["7"]["inputs"]["text"] == "blurry"
    assert wf["3"]["inputs"]["seed"] == 42 and wf["3"]["inputs"]["steps"] == 4
    assert wf["5"]["inputs"]["width"] == 768 and wf["5"]["inputs"]["height"] == 512


async def test_generate_unknown_model_raises_rather_than_running_the_wrong_graph() -> None:
    import dataclasses

    gen = _client(lambda r: httpx.Response(404))
    with pytest.raises(ImageGenError):
        await gen.generate(dataclasses.replace(GEN, model="not-a-real-model"))


async def test_negative_prompt_fills_the_negative_node_in_both_graphs() -> None:
    """A negative prompt lands on the negative encoder — node 7 (generate, CLIPTextEncode)
    and node 69 (edit, the TextEncodeQwenImageEditPlus sibling of the positive 68)."""
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "in.png"})
        if request.url.path == "/prompt":
            seen["wf"] = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"prompt_id": "abc123"})
        if request.url.path == "/history/abc123":
            return httpx.Response(200, json=_HISTORY_DONE)
        return httpx.Response(200, content=PNG)

    import dataclasses

    await _client(handle).generate(dataclasses.replace(GEN, negative_prompt="blurry, text"))
    assert seen["wf"]["7"]["inputs"]["text"] == "blurry, text"
    assert seen["wf"]["6"]["inputs"]["text"] == "a cat"  # positive untouched

    await _client(handle).edit(dataclasses.replace(EDIT, negative_prompt="watermark"), b"src")
    assert seen["wf"]["69"]["inputs"]["prompt"] == "watermark"
    assert seen["wf"]["68"]["inputs"]["prompt"] == EDIT.prompt  # positive untouched


async def test_edit_uploads_source_then_renders() -> None:
    uploaded: list[bytes] = []
    referenced: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            uploaded.append(request.content)
            return httpx.Response(200, json={"name": "input.png", "subfolder": "", "type": "input"})
        if request.url.path == "/prompt":
            graph = json.loads(request.content)["prompt"]
            referenced["image"] = graph["41"]["inputs"]["image"]
            referenced["megapixels"] = graph["79"]["inputs"]["megapixels"]
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
    # The resolution's total-pixel budget reached the scale node.
    assert referenced["megapixels"] == 1.6


async def test_edit_with_reference_images_uploads_and_wires_each_encoder() -> None:
    """Multi-image edit: every image is uploaded, and each reference is wired into the
    image{n} slot of BOTH TextEncodeQwenImageEditPlus encoders via its own LoadImage→scale
    pair — the primary stays image1 (the latent base)."""
    uploaded: list[bytes] = []
    graph: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            uploaded.append(request.content)
            # Distinct server names per upload so the wiring is unambiguous.
            return httpx.Response(200, json={"name": f"in{len(uploaded)}.png"})
        if request.url.path == "/prompt":
            graph.update(json.loads(request.content)["prompt"])
            return httpx.Response(200, json={"prompt_id": "abc123"})
        if request.url.path == "/history/abc123":
            return httpx.Response(200, json=_HISTORY_DONE)
        return httpx.Response(200, content=PNG)

    out = await _client(handle).edit(EDIT, b"primary", extra_sources=[b"ref-a", b"ref-b"])
    assert out == PNG
    assert len(uploaded) == 3  # primary + 2 references each uploaded
    encoders = [n for n in graph.values() if n["class_type"] == "TextEncodeQwenImageEditPlus"]
    assert len(encoders) == 2  # positive + negative
    for enc in encoders:
        # image1 is the primary's scaled output (node 79); image2/image3 are the references'.
        assert "image2" in enc["inputs"] and "image3" in enc["inputs"]
        scale2, scale3 = enc["inputs"]["image2"][0], enc["inputs"]["image3"][0]
        assert graph[scale2]["class_type"] == "ImageScaleToTotalPixels"
        load2 = graph[scale2]["inputs"]["image"][0]
        assert graph[load2]["class_type"] == "LoadImage"
        assert graph[load2]["inputs"]["image"] == "in2.png"
        assert graph[graph[scale3]["inputs"]["image"][0]]["inputs"]["image"] == "in3.png"


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


# --- the live (WebSocket) path --------------------------------------------------

PID = "abc123"


def _prog(value: int, total: int, prompt_id: str = PID) -> str:
    data = {"value": value, "max": total, "prompt_id": prompt_id}
    return json.dumps({"type": "progress", "data": data})


def _executing_null(prompt_id: str = PID) -> str:
    return json.dumps({"type": "executing", "data": {"node": None, "prompt_id": prompt_id}})


def _preview_frame(image: bytes) -> bytes:
    # ComfyUI binary preview: [uint32 event=1][uint32 image-type=1(JPEG)][bytes].
    return (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + image


class _FakeWs:
    """An async-iterable of scripted ws frames (str text / bytes binary)."""

    def __init__(self, frames: list[str | bytes]) -> None:
        self._frames = frames

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        async def gen() -> AsyncIterator[str | bytes]:
            for f in self._frames:
                yield f

        return gen()


def _bare() -> ComfyUiImageGen:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    return ComfyUiImageGen(BASE, http)


async def test_drive_ws_emits_every_step_with_latest_preview() -> None:
    ticks: list[tuple[int, int, bytes | None]] = []
    frames: list[str | bytes] = [
        _prog(1, 4),  # step 1 — before any preview frame
        _preview_frame(b"jpeg-a"),
        _prog(2, 4),  # step 2 — preview now present
        _prog(2, 4),  # a duplicate of the same step — suppressed, not a redundant tick
        _preview_frame(b"jpeg-b"),
        _prog(3, 4),  # step 3 — newest preview
        _prog(4, 4),  # step 4
        _executing_null(),
    ]
    await _bare()._drive_ws(_FakeWs(frames), PID, lambda s, t, p: ticks.append((s, t, p)))
    # One emit per sampler step, each carrying the most recent preview (None until one
    # lands); a repeated step value does not re-tick.
    assert ticks == [(1, 4, None), (2, 4, b"jpeg-a"), (3, 4, b"jpeg-b"), (4, 4, b"jpeg-b")]


async def test_drive_ws_ignores_another_runs_messages() -> None:
    ticks: list[int] = []
    frames: list[str | bytes] = [_prog(10, 20, "other-run"), _prog(5, 20), _executing_null()]
    await _bare()._drive_ws(_FakeWs(frames), PID, lambda s, t, p: ticks.append(s))
    assert ticks == [5]  # the foreign prompt_id's progress was skipped


async def test_drive_ws_raises_interrupted_on_stop() -> None:
    frames: list[str | bytes] = [
        _prog(5, 20),
        json.dumps({"type": "execution_interrupted", "data": {"prompt_id": PID}}),
    ]
    with pytest.raises(ImageGenInterrupted):
        await _bare()._drive_ws(_FakeWs(frames), PID, lambda s, t, p: None)


async def test_drive_ws_raises_on_execution_error() -> None:
    err = json.dumps({"type": "execution_error", "data": {"prompt_id": PID, "msg": "boom"}})
    with pytest.raises(ImageGenError):
        await _bare()._drive_ws(_FakeWs([err]), PID, lambda s, t, p: None)


def test_parse_preview_reads_jpeg_after_the_header() -> None:
    assert _parse_preview(_preview_frame(b"the-jpeg")) == b"the-jpeg"
    # event type 2 (not PREVIEW_IMAGE), and a too-short frame, both yield None.
    assert _parse_preview((2).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"x") is None
    assert _parse_preview(b"short") is None


def test_ws_url_swaps_scheme_and_carries_client_id() -> None:
    assert _bare()._ws_url("cid") == "ws://comfyui:8188/ws?clientId=cid"
    https = ComfyUiImageGen("https://comfyui:8188", httpx.AsyncClient())
    assert https._ws_url("cid") == "wss://comfyui:8188/ws?clientId=cid"


async def test_generate_over_websocket_drives_progress_and_returns_final() -> None:
    # End-to-end live path against a real loopback ws server (scripted frames) + a
    # mocked HTTP transport for /prompt + /history + /view.
    frames: list[str | bytes] = [
        _prog(5, 20),
        _preview_frame(b"jpg"),
        _prog(20, 20),
        _executing_null(),
    ]

    async def handler(ws: object) -> None:
        for f in frames:
            await ws.send(f)  # type: ignore[attr-defined]
        # Buffered frames (incl. executing-null) are read before the close lands, so
        # the driver breaks on completion; closing here avoids a lingering handler.

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": PID})
            if request.url.path == f"/history/{PID}":
                return httpx.Response(200, json=_HISTORY_DONE)
            return httpx.Response(200, content=PNG)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        gen = ComfyUiImageGen(f"http://127.0.0.1:{port}", http, sleep=_no_sleep_module)
        ticks: list[tuple[int, int, bytes | None]] = []
        out = await gen.generate(GEN, on_progress=lambda s, t, p: ticks.append((s, t, p)))
        assert out == PNG
        assert [(s, t) for s, t, _ in ticks] == [(5, 20), (20, 20)]


async def _no_sleep_module(_: float) -> None:
    return None


async def test_fake_image_gen_returns_valid_png_and_records_specs() -> None:
    fake = FakeImageGen()
    gen_bytes = await fake.generate(GEN)
    assert gen_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert fake.last_gen == GEN

    source = b"\x89PNG\r\n\x1a\nsource"
    edit_bytes = await fake.edit(EDIT, source)
    assert edit_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert fake.last_edit == EDIT and fake.last_source == source
