"""ComfyUI HTTP client for local Qwen-Image generation (docs/IMAGE_GEN_PLAN.md).

The graph SHAPE lives in the JSON workflow templates, not here: this client is a
generic ComfyUI driver that loads a template, fills a fixed set of well-known
node slots, submits, polls for completion, and fetches the result PNG. The
workflow JSON + the node-id/key constants below are the host-validated
integration seam — they cannot be checked against a live ComfyUI from CI, so they
are deliberately small and owner-tuned on the Strix Halo box. Keeping the driver
graph-agnostic means a model/graph swap is a JSON edit, not a code change.

All HTTP rides an injected `httpx.AsyncClient` (the app's shared client) so tests
drive it through a MockTransport with no network (DEVELOPMENT.md "no network in
tests").
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Any, Protocol
from uuid import uuid4

import httpx
import structlog
import websockets
from websockets.exceptions import InvalidURI, WebSocketException

log = structlog.get_logger()

# A live-progress callback the WebSocket path calls as sampling advances:
# (step, total_steps, latest_preview_jpeg_or_None). Sync + best-effort — the tool
# turns it into an ephemeral ToolProgressEvent; it must never raise into the driver.
OnProgress = Callable[[int, int, bytes | None], None]

# ComfyUI's binary preview frame: [uint32 event][uint32 image-type][image bytes];
# event type 1 is PREVIEW_IMAGE.
_WS_PREVIEW_EVENT = 1

# A 1328x1328 20-step Qwen-Image takes ~3.5 min on the Strix Halo iGPU, and the
# first call also pays a one-time model load; budget generously so a real run
# never times out mid-render. The poll loop returns as soon as the image lands,
# so a large ceiling costs nothing on fast runs.
DEFAULT_TIMEOUT = 600.0
DEFAULT_POLL_INTERVAL = 1.5

# The workflow templates and, per template, the node ids the driver fills. The
# gen and edit graphs are *different* ComfyUI graphs, so the bindings are
# per-template, not global. These mirror the JSON in workflows/ and are the
# host-validated seam (see module docs): the gen binding matches the Qwen-Image
# graph exported from the Strix Halo box.
_GEN_TEMPLATE = "qwen_image.json"
_EDIT_TEMPLATE = "qwen_image_edit.json"


@dataclass(frozen=True)
class _Binding:
    """Which node ids in a template hold the slots the driver overwrites.

    `latent` is generate-only (the edit graph derives its latent from the
    uploaded source); `input_image` is edit-only (the LoadImage node)."""

    prompt: str  # the positive prompt node — the negative node is left untouched
    sampler: str  # KSampler — holds seed + steps
    latent: str | None = None  # Empty*LatentImage — width/height (generate only)
    input_image: str | None = None  # LoadImage — server-side name (edit only)
    total_pixels: str | None = None  # ImageScaleToTotalPixels — megapixels (edit only)
    # The prompt node's text input key differs by graph: CLIPTextEncode uses
    # "text", the edit graph's TextEncodeQwenImageEditPlus uses "prompt".
    prompt_key: str = "text"


# Qwen-Image text->image, validated on the Strix Halo box: prompt=6, KSampler=3,
# EmptySD3LatentImage=58 (the loaders + ModelSamplingAuraFlow are left as authored).
_GEN_BINDING = _Binding(prompt="6", sampler="3", latent="58")
# Qwen-Image-Edit image->image, exported from the box: the prompt is a
# TextEncodeQwenImageEditPlus (68, key "prompt"), KSampler is 65, LoadImage is 41,
# and ImageScaleToTotalPixels (79) sets the output's total-pixel budget. The rest of
# the reference-latent pipeline (VAEEncode->FluxKontext) is left as authored.
_EDIT_BINDING = _Binding(
    prompt="68", sampler="65", input_image="41", total_pixels="79", prompt_key="prompt"
)

_INPUT_IMAGE_KEY = "image"

# Qwen-Image-Edit-2511's multi-image text encoder: it carries image1..image3 slots, so a
# reference image is wired into every encoder node's image{n}. Found by class (not a fixed
# id) so the positive AND negative encoders both get each reference.
_EDIT_ENCODER_CLASS = "TextEncodeQwenImageEditPlus"
# Up to 3 images total: the primary (edited / latent base) plus up to 2 extra references.
MAX_EDIT_IMAGES = 3


class ImageGenError(Exception):
    """A generation failed — ComfyUI unreachable, a non-2xx response, an error
    payload, or no output node. Handlers turn this into a clean tool-error string,
    never a stack trace to the model."""


class ImageGenTimeout(ImageGenError):
    """Generation did not complete within the overall wait budget."""


class ImageGenInterrupted(ImageGenError):
    """The render was stopped (ComfyUI /interrupt) — the owner hit Stop. The tool
    turns this into a clean 'stopped, nothing saved' result, not a failure."""


@dataclass(frozen=True)
class GenSpec:
    """A text→image request. `seed` is the resolved seed (random seeds are chosen
    upstream and recorded, so a result is repeatable)."""

    prompt: str
    width: int
    height: int
    steps: int
    seed: int
    model: str


@dataclass(frozen=True)
class EditSpec:
    """An image→image request; the source bytes ride alongside, not in the spec."""

    prompt: str
    width: int
    height: int
    steps: int
    seed: int
    model: str
    megapixels: float  # the output's total-pixel budget (the source is scaled to it)


class ImageGen(Protocol):
    async def generate(
        self, spec: GenSpec, on_progress: OnProgress | None = None
    ) -> bytes: ...  # PNG bytes

    async def edit(
        self,
        spec: EditSpec,
        source: bytes,
        on_progress: OnProgress | None = None,
        *,
        extra_sources: Sequence[bytes] = (),
    ) -> bytes: ...  # PNG bytes — `source` is primary; `extra_sources` are references


def _load_template(name: str) -> dict[str, Any]:
    """Load a workflow template fresh per call so a filled copy is never shared."""
    raw = (resources.files("jbrain.image_gen") / "workflows" / name).read_text(encoding="utf-8")
    return json.loads(raw)


class ComfyUiImageGen:
    """Drive a localhost ComfyUI over its HTTP API.

    `client` is the app's shared `httpx.AsyncClient` (a single on-box host, no
    auth — ComfyUI is host-managed, not containerized by JBrain2). `monotonic`
    and `sleep` are injected so the timeout path is testable without real waits.
    """

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._base = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._sleep = sleep

    async def generate(self, spec: GenSpec, on_progress: OnProgress | None = None) -> bytes:
        workflow = _load_template(_GEN_TEMPLATE)
        self._fill_common(workflow, spec, _GEN_BINDING)
        latent_node = _GEN_BINDING.latent
        assert latent_node is not None  # the gen graph always carries a latent node
        latent = workflow[latent_node]["inputs"]
        latent["width"] = spec.width
        latent["height"] = spec.height
        return await self._run(workflow, on_progress)

    async def edit(
        self,
        spec: EditSpec,
        source: bytes,
        on_progress: OnProgress | None = None,
        *,
        extra_sources: Sequence[bytes] = (),
    ) -> bytes:
        server_name = await self._upload_input(source)
        workflow = _load_template(_EDIT_TEMPLATE)
        self._fill_common(workflow, spec, _EDIT_BINDING)
        image_node = _EDIT_BINDING.input_image
        assert image_node is not None  # the edit graph always carries a LoadImage node
        workflow[image_node]["inputs"][_INPUT_IMAGE_KEY] = server_name
        scale_node = _EDIT_BINDING.total_pixels
        assert scale_node is not None  # the edit graph always carries the scale node
        workflow[scale_node]["inputs"]["megapixels"] = spec.megapixels
        # Each extra reference (Qwen-Image-Edit-2511 takes up to 3 images total) gets its own
        # LoadImage→scale pair wired into the encoders' image2/image3; the primary above stays
        # the latent base, so references are added conditioning, not what's being edited.
        for index, extra in enumerate(extra_sources, start=2):
            name = await self._upload_input(extra)
            self._add_reference_image(workflow, name, index, spec.megapixels, scale_node)
        return await self._run(workflow, on_progress)

    @staticmethod
    def _add_reference_image(
        workflow: dict[str, Any],
        server_name: str,
        index: int,
        megapixels: float,
        scale_template: str,
    ) -> None:
        """Add a LoadImage→ImageScaleToTotalPixels pair for one reference image and wire its
        scaled output into the image{index} slot of every prompt encoder. The scale node's
        settings are cloned from the primary's so every image is sized the same way."""
        load_id = f"jbrain_ref_load_{index}"
        scale_id = f"jbrain_ref_scale_{index}"
        workflow[load_id] = {"class_type": "LoadImage", "inputs": {_INPUT_IMAGE_KEY: server_name}}
        scale_inputs = dict(workflow[scale_template]["inputs"])
        scale_inputs["image"] = [load_id, 0]
        scale_inputs["megapixels"] = megapixels
        workflow[scale_id] = {"class_type": "ImageScaleToTotalPixels", "inputs": scale_inputs}
        for node in workflow.values():
            if node.get("class_type") == _EDIT_ENCODER_CLASS:
                node["inputs"][f"image{index}"] = [scale_id, 0]

    async def _run(self, workflow: dict[str, Any], on_progress: OnProgress | None) -> bytes:
        """Submit + await the final image. With `on_progress` we drive ComfyUI's
        WebSocket for live step/preview ticks; without one we use the plain
        submit→poll path (the fake + every existing caller)."""
        if on_progress is None:
            prompt_id = await self._submit(workflow)
            return await self._await(prompt_id)
        return await self._run_ws(workflow, on_progress)

    async def _run_ws(self, workflow: dict[str, Any], on_progress: OnProgress) -> bytes:
        """Connect ComfyUI's /ws FIRST (so no early frames are missed), submit under
        the same client_id, drive `on_progress` from the progress + preview frames
        until the run ends, then fetch the final image over HTTP (already complete)."""
        client_id = uuid4().hex
        prompt_id: str | None = None
        try:
            async with websockets.connect(
                self._ws_url(client_id), open_timeout=self._timeout
            ) as ws:
                prompt_id = await self._submit(workflow, client_id=client_id)
                await asyncio.wait_for(self._drive_ws(ws, prompt_id, on_progress), self._timeout)
        except ImageGenError:
            raise  # interrupt / execution error already shaped
        except TimeoutError as exc:
            raise ImageGenTimeout(f"ComfyUI did not finish within {self._timeout:g}s") from exc
        except (OSError, InvalidURI, WebSocketException) as exc:
            raise ImageGenError(f"ComfyUI websocket failed: {exc}") from exc
        assert prompt_id is not None  # set before any normal exit from the `with`
        return await self._await(prompt_id)

    async def _drive_ws(
        self, ws: AsyncIterable[str | bytes], prompt_id: str, on_progress: OnProgress
    ) -> None:
        """Relay sampling progress: emit `on_progress` once per sampler step with the
        latest preview, and return when our prompt finishes (executing → node null).
        Raises ImageGenInterrupted on a stop, ImageGenError on a run error.

        Per-step is cheap: ComfyUI already decodes a preview (a fast TAESD/latent2rgb
        approximation, not the full VAE) and sends a progress + b_preview frame every
        step over /ws, so relaying each one is just a base64 + SSE — no extra render
        cost. We only suppress a repeat at the same step so a duplicate message isn't a
        redundant tick."""
        last_value = -1
        preview: bytes | None = None
        async for raw in ws:
            if isinstance(raw, bytes | bytearray):
                pv = _parse_preview(bytes(raw))
                if pv is not None:
                    preview = pv
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            data = msg.get("data") or {}
            # ComfyUI tags most messages with the prompt_id; skip another run's.
            if data.get("prompt_id") not in (None, prompt_id):
                continue
            mtype = msg.get("type")
            if mtype == "progress":
                total = int(data.get("max") or 0)
                value = int(data.get("value") or 0)
                if total > 0 and value != last_value:
                    last_value = value
                    on_progress(value, total, preview)
            elif mtype == "execution_interrupted":
                raise ImageGenInterrupted("the render was stopped")
            elif mtype == "execution_error":
                raise ImageGenError(f"ComfyUI run failed: {data!r}")
            elif mtype == "executing" and data.get("node") is None:
                return  # our prompt finished executing

    def _ws_url(self, client_id: str) -> str:
        """The /ws URL for the base http(s) URL, carrying the client_id ComfyUI keys
        this connection's messages to."""
        base = self._base
        if base.startswith("https"):
            base = "wss" + base[len("https") :]
        elif base.startswith("http"):
            base = "ws" + base[len("http") :]
        return f"{base}/ws?clientId={client_id}"

    def _fill_common(
        self, workflow: dict[str, Any], spec: GenSpec | EditSpec, binding: _Binding
    ) -> None:
        workflow[binding.prompt]["inputs"][binding.prompt_key] = spec.prompt
        sampler = workflow[binding.sampler]["inputs"]
        sampler["seed"] = spec.seed
        sampler["steps"] = spec.steps

    async def _upload_input(self, data: bytes) -> str:
        """POST the source PNG; ComfyUI returns the server-side name to reference
        from the LoadImage node in the edit graph."""
        files = {"image": ("input.png", data, "image/png")}
        try:
            resp = await self._client.post(f"{self._base}/upload/image", files=files)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageGenError("could not upload the source image to ComfyUI") from exc
        name = body.get("name") if isinstance(body, dict) else None
        if not name:
            raise ImageGenError("ComfyUI upload returned no image name")
        return str(name)

    async def _submit(self, workflow: dict[str, Any], client_id: str | None = None) -> str:
        """POST the filled graph; ComfyUI queues it and returns a prompt_id to poll.
        `client_id` ties the run's WebSocket messages to an open /ws (the live path)."""
        payload = {"prompt": workflow, "client_id": client_id or uuid4().hex}
        try:
            resp = await self._client.post(f"{self._base}/prompt", json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageGenError("could not submit the workflow to ComfyUI") from exc
        prompt_id = body.get("prompt_id") if isinstance(body, dict) else None
        if not prompt_id:
            # ComfyUI rejects an invalid graph with a `node_errors`/`error` body.
            raise ImageGenError(f"ComfyUI rejected the workflow: {body!r}")
        return str(prompt_id)

    async def _await(self, prompt_id: str) -> bytes:
        """Poll /history until the run's outputs carry an image, then fetch it.

        Bounded by the overall timeout; each empty poll sleeps `poll_interval`."""
        deadline = self._monotonic() + self._timeout
        while True:
            image_ref = await self._poll_once(prompt_id)
            if image_ref is not None:
                return await self._fetch_view(image_ref)
            if self._monotonic() >= deadline:
                raise ImageGenTimeout(f"ComfyUI did not finish within {self._timeout:g}s")
            await self._sleep(self._poll_interval)

    async def _poll_once(self, prompt_id: str) -> dict[str, Any] | None:
        """Return the first output image ref once present, else None (keep polling)."""
        try:
            resp = await self._client.get(f"{self._base}/history/{prompt_id}")
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageGenError("could not read the ComfyUI run history") from exc
        entry = body.get(prompt_id) if isinstance(body, dict) else None
        if not isinstance(entry, dict):
            return None  # not in history yet — still queued/running
        # A node that errors mid-run leaves an error status and empty outputs;
        # surface it now rather than polling out the whole timeout budget.
        status = entry.get("status")
        if isinstance(status, dict) and status.get("status_str") == "error":
            raise ImageGenError(f"ComfyUI run failed: {status!r}")
        outputs = entry.get("outputs")
        if not isinstance(outputs, dict):
            return None
        for node_output in outputs.values():
            images = node_output.get("images") if isinstance(node_output, dict) else None
            if isinstance(images, list) and images:
                first = images[0]
                if isinstance(first, dict) and first.get("filename"):
                    return first
        return None

    async def _fetch_view(self, image_ref: dict[str, Any]) -> bytes:
        """GET the rendered PNG bytes for an output image ref."""
        params = {
            "filename": image_ref.get("filename", ""),
            "subfolder": image_ref.get("subfolder", ""),
            "type": image_ref.get("type", "output"),
        }
        try:
            resp = await self._client.get(f"{self._base}/view", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ImageGenError("could not fetch the generated image from ComfyUI") from exc
        if not resp.content:
            raise ImageGenError("ComfyUI returned an empty image body")
        return resp.content


def _parse_preview(frame: bytes) -> bytes | None:
    """The JPEG/PNG bytes of a ComfyUI binary preview frame, or None for a frame
    that isn't a preview (or is too short to carry the 8-byte header)."""
    if len(frame) < 8:
        return None
    if int.from_bytes(frame[0:4], "big") != _WS_PREVIEW_EVENT:
        return None
    return frame[8:]
