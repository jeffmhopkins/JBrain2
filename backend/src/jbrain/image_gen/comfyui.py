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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any, Protocol
from uuid import uuid4

import httpx
import structlog

log = structlog.get_logger()

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

    prompt: str  # positive CLIPTextEncode — the negative node is left untouched
    sampler: str  # KSampler — holds seed + steps
    latent: str | None = None  # Empty*LatentImage — width/height (generate only)
    input_image: str | None = None  # LoadImage — server-side name (edit only)


# Qwen-Image text->image, validated on the Strix Halo box: prompt=6, KSampler=3,
# EmptySD3LatentImage=58 (the loaders + ModelSamplingAuraFlow are left as authored).
_GEN_BINDING = _Binding(prompt="6", sampler="3", latent="58")
# Qwen-Image-Edit image->image — placeholder ids, awaiting its own on-box export.
_EDIT_BINDING = _Binding(prompt="2", sampler="5", input_image="8")

_PROMPT_KEY = "text"
_INPUT_IMAGE_KEY = "image"


class ImageGenError(Exception):
    """A generation failed — ComfyUI unreachable, a non-2xx response, an error
    payload, or no output node. Handlers turn this into a clean tool-error string,
    never a stack trace to the model."""


class ImageGenTimeout(ImageGenError):
    """Generation did not complete within the overall wait budget."""


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


class ImageGen(Protocol):
    async def generate(self, spec: GenSpec) -> bytes: ...  # PNG bytes

    async def edit(self, spec: EditSpec, source: bytes) -> bytes: ...  # PNG bytes


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

    async def generate(self, spec: GenSpec) -> bytes:
        workflow = _load_template(_GEN_TEMPLATE)
        self._fill_common(workflow, spec, _GEN_BINDING)
        latent_node = _GEN_BINDING.latent
        assert latent_node is not None  # the gen graph always carries a latent node
        latent = workflow[latent_node]["inputs"]
        latent["width"] = spec.width
        latent["height"] = spec.height
        prompt_id = await self._submit(workflow)
        return await self._await(prompt_id)

    async def edit(self, spec: EditSpec, source: bytes) -> bytes:
        server_name = await self._upload_input(source)
        workflow = _load_template(_EDIT_TEMPLATE)
        self._fill_common(workflow, spec, _EDIT_BINDING)
        image_node = _EDIT_BINDING.input_image
        assert image_node is not None  # the edit graph always carries a LoadImage node
        workflow[image_node]["inputs"][_INPUT_IMAGE_KEY] = server_name
        prompt_id = await self._submit(workflow)
        return await self._await(prompt_id)

    def _fill_common(
        self, workflow: dict[str, Any], spec: GenSpec | EditSpec, binding: _Binding
    ) -> None:
        workflow[binding.prompt]["inputs"][_PROMPT_KEY] = spec.prompt
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

    async def _submit(self, workflow: dict[str, Any]) -> str:
        """POST the filled graph; ComfyUI queues it and returns a prompt_id to poll."""
        payload = {"prompt": workflow, "client_id": uuid4().hex}
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
