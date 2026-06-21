"""jerv's image-generation tools: `generate_image` (text→image) and `edit_image`
(image→image), thin handlers over the `jbrain.image_gen` adapter (docs/IMAGE_GEN_PLAN.md).

These are `web`-class, jerv-only, direct-exec tools (the `current_location` precedent):
on-box ComfyUI, no egress despite the class name. Each handler resolves the request to a
spec, drives the (faked-in-tests) image model, stores the result PNG through the blob store
(CLAUDE.md rule 2), and records one immutable `generated_images` row under the caller's
RLS-scoped session (rule 3). A generation failure becomes a clean tool-error string — never
a stack trace to the model. The result rides back as a data-only `generated_image` view; the
app builds the `<img>` src from the row id, so the model never authors a URL (invariant #9).
"""

from __future__ import annotations

import base64
import secrets
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import scoped_session
from jbrain.image_gen.comfyui import (
    EditSpec,
    GenSpec,
    ImageGen,
    ImageGenError,
    ImageGenInterrupted,
    OnProgress,
)
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.models.images import GeneratedImage, GeneratedImageRepo
from jbrain.storage import BlobStore

if TYPE_CHECKING:
    from jbrain.agent.attachments import TurnAttachmentRepo

log = structlog.get_logger()

# The full image model; the edit sibling is selected per-kind.
_GEN_MODEL = "qwen-image-2512"
_EDIT_MODEL = "qwen-image-edit"

_DEFAULT_STEPS = 20
# A bigint seed: positive and within the model's accepted range (random when absent).
_SEED_BITS = 63

# aspect → (long_frac, short_frac) of a resolution's edge: square is 1:1, the other
# two a 3:4 portrait/landscape. Scaled by the edge below they land on multiples of 64.
_ASPECTS: dict[str, tuple[float, float]] = {
    "square": (1.0, 1.0),
    "portrait": (0.75, 1.0),
    "landscape": (1.0, 0.75),
}
_DEFAULT_ASPECT = "square"

# resolution → (generate square-edge px, edit total-megapixels). Medium is the model's
# native ~1 MP and the default; `small` cuts the activation/VAE-decode memory peak (the
# weights are fixed, but decode memory scales with pixel count) for headroom on a tight
# unified-memory box; `large` trades that headroom back for more detail.
_RESOLUTIONS: dict[str, tuple[int, float]] = {
    "small": (768, 0.9),
    "medium": (1024, 1.6),
    "large": (1280, 2.5),
}
_DEFAULT_RESOLUTION = "medium"


def _snap64(value: float) -> int:
    """Round to the nearest multiple of 64 (>=64) — the latent grid Qwen expects."""
    return max(64, round(value / 64) * 64)


def _dims(aspect: str | None, resolution: str | None) -> tuple[int, int] | None:
    """The (w, h) in px for an aspect at a resolution, snapped to multiples of 64;
    None when either the aspect or the resolution is unrecognized (handler errors)."""
    ratio = _ASPECTS.get(aspect or _DEFAULT_ASPECT)
    res = _RESOLUTIONS.get(resolution or _DEFAULT_RESOLUTION)
    if ratio is None or res is None:
        return None
    edge = res[0]
    long_frac, short_frac = ratio
    return _snap64(edge * long_frac), _snap64(edge * short_frac)


def _megapixels(resolution: str | None) -> float:
    """The edit path's total-pixel budget for a (pre-validated) resolution."""
    return _RESOLUTIONS[resolution or _DEFAULT_RESOLUTION][1]


def _resolve_seed(raw: object) -> int:
    """The seed to use AND record: the caller's value when given, else a fresh random
    one (so a random result stays reproducible)."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return secrets.randbits(_SEED_BITS)


def _resolve_steps(raw: object) -> int:
    """A positive step count; the default when absent or nonsensical."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return _DEFAULT_STEPS


_STOPPED_MESSAGE = "Stopped the render — nothing was saved."


async def _free_local_llms(gateway: LocalGateway) -> None:
    """Time-share unified memory: unload any resident local LLM before a render.

    On a single unified-memory box (Strix Halo) the LLM that drove this turn (~tens
    of GB) and the diffusion model (~39 GB bf16 on ROCm) can't both fit alongside the
    VAE decode, so the image OOMs. We free the LLM now; the agent loop's NEXT call —
    to compose the reply after this tool returns — transparently reloads it via
    llama-swap's on-demand loading (no explicit reload needed, so the image still
    surfaces promptly and the reload is just the usual "composing" wait).

    Best-effort: a cloud-driven turn (nothing resident) or an unreachable gateway is a
    clean no-op — never let memory housekeeping fail the generation."""
    try:
        for served in await gateway.running():
            await gateway.unload(served)
    except LocalGatewayError as exc:
        log.info("image_gen.llm_unload_skipped", error=str(exc))


def _progress_callback(ctx: ToolContext) -> OnProgress | None:
    """Bridge the driver's (step, total, preview_bytes) ticks to the turn's progress
    sink, base-64ing each ephemeral preview into a data URI the PWA shows inline.
    None when the turn has no sink (the batch path) — the driver then skips its
    WebSocket entirely and just polls for the final image."""
    sink = ctx.emit_progress
    if sink is None:
        return None

    def on_progress(step: int, total: int, preview: bytes | None) -> None:
        uri = f"data:image/jpeg;base64,{base64.b64encode(preview).decode()}" if preview else None
        sink(step, total, uri)

    return on_progress


def generated_image_view(row: GeneratedImage) -> ViewPayload:
    """The data-only twin of the tool's prose: a `generated_image` view the app renders
    inline. NO url — the component builds the `<img>` src from `image_id` (invariant #9).
    Ids/dims are JSON-safe scalars (the uuid is stringified)."""
    return ViewPayload(
        view="generated_image",
        surface="inline",
        data={
            "image_id": str(row.id),
            "kind": row.kind,
            "prompt": row.prompt,
            "width": row.width,
            "height": row.height,
            "model": row.model,
        },
    )


def build_image_handlers(
    imagegen: ImageGen,
    blob_store: BlobStore,
    repo: GeneratedImageRepo,
    attachments: TurnAttachmentRepo,
    maker: async_sessionmaker[AsyncSession],
    local_gateway: LocalGateway,
) -> dict[str, ToolHandler]:
    """`generate_image` + `edit_image`. Wired only when image generation is configured
    (a localhost ComfyUI); the registry omits both tools otherwise (graceful degrade).

    `maker` opens the RLS-scoped transaction each write/read runs under (the repo takes an
    already-scoped `AsyncSession`); the firewall is Postgres', applied from `ctx.session`."""

    async def generate_image_tool(arguments: dict, ctx: ToolContext) -> str:
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return "generate_image needs a prompt."
        aspect, resolution = arguments.get("aspect"), arguments.get("resolution")
        dims = _dims(aspect, resolution)
        if dims is None:
            if (aspect or _DEFAULT_ASPECT) not in _ASPECTS:
                return "aspect must be one of: square, portrait, landscape."
            return "resolution must be one of: small, medium, large."
        width, height = dims
        seed = _resolve_seed(arguments.get("seed"))
        steps = _resolve_steps(arguments.get("steps"))
        spec = GenSpec(
            prompt=prompt, width=width, height=height, steps=steps, seed=seed, model=_GEN_MODEL
        )
        await _free_local_llms(local_gateway)
        try:
            png = await imagegen.generate(spec, _progress_callback(ctx))
        except ImageGenInterrupted:
            return _STOPPED_MESSAGE
        except ImageGenError as exc:
            log.warning("generate_image_failed", error=str(exc))
            return "I couldn't generate that image right now — the image model didn't respond."
        sha = await blob_store.put(png)
        async with scoped_session(maker, ctx.session) as session:
            row = await repo.insert(
                session,
                blob_sha256=sha,
                kind="generate",
                model=_GEN_MODEL,
                prompt=prompt,
                source_sha256=None,
                width=width,
                height=height,
                steps=steps,
                seed=seed,
            )
        return ToolOutput(
            f"Generated a {width}x{height} image (seed {seed}); the app is showing it.",
            view=generated_image_view(row),
        )

    async def _source_bytes(arguments: dict, ctx: ToolContext) -> tuple[bytes, str] | str:
        """Resolve EXACTLY ONE source to (bytes, sha) or a clean error string. Both/neither
        is rejected before any spend; an unknown/out-of-scope id is a clean miss (RLS-scoped
        — a foreign artifact simply isn't visible)."""
        image_id = str(arguments.get("source_image_id", "")).strip()
        attachment_id = str(arguments.get("source_attachment_id", "")).strip()
        if bool(image_id) == bool(attachment_id):
            return (
                "edit_image needs exactly one source: source_image_id (an image you generated)"
                " or source_attachment_id (an image the owner attached) — not both, not neither."
            )
        if image_id:
            # generated_images is owner-only, so jerv's empty-scope owner context reads it.
            async with scoped_session(maker, ctx.session) as session:
                row = await repo.get(session, image_id)
            if row is None:
                return "No generated image with that id is in this chat."
            try:
                return await blob_store.get(row.blob_sha256), row.blob_sha256
            except FileNotFoundError:
                return "That source image is no longer available."
        # A chat attachment is DOMAIN-scoped (stamped 'general' for a jerv session), so it
        # is read under the attachment context (the session's scopes + that stamped domain),
        # not jerv's empty read scopes — the same widening the chat turn uses to load
        # attachments. RLS still hides a foreign-domain id, which reads as a clean miss.
        if ctx.agent_session_id is None:
            return "No attached image with that id is in this chat."
        att_ctx = await attachments.session_read_context(ctx.session, ctx.agent_session_id)
        if att_ctx is None:
            return "No attached image with that id is in this chat."
        info = await attachments.get(att_ctx, attachment_id)
        if info is None:
            return "No attached image with that id is in this chat."
        try:
            return await blob_store.get(info.sha256), info.sha256
        except FileNotFoundError:
            return "That source image is no longer available."

    async def edit_image_tool(arguments: dict, ctx: ToolContext) -> str:
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return "edit_image needs a prompt (the edit instruction)."
        aspect, resolution = arguments.get("aspect"), arguments.get("resolution")
        dims = _dims(aspect, resolution)
        if dims is None:
            if (aspect or _DEFAULT_ASPECT) not in _ASPECTS:
                return "aspect must be one of: square, portrait, landscape."
            return "resolution must be one of: small, medium, large."
        source = await _source_bytes(arguments, ctx)
        if isinstance(source, str):
            return source  # a clean error — no row, no spend
        source_bytes, source_sha = source
        width, height = dims
        seed = _resolve_seed(arguments.get("seed"))
        steps = _resolve_steps(arguments.get("steps"))
        spec = EditSpec(
            prompt=prompt,
            width=width,
            height=height,
            steps=steps,
            seed=seed,
            model=_EDIT_MODEL,
            megapixels=_megapixels(resolution),
        )
        await _free_local_llms(local_gateway)
        try:
            png = await imagegen.edit(spec, source_bytes, _progress_callback(ctx))
        except ImageGenInterrupted:
            return _STOPPED_MESSAGE
        except ImageGenError as exc:
            log.warning("edit_image_failed", error=str(exc))
            return "I couldn't edit that image right now — the image model didn't respond."
        sha = await blob_store.put(png)
        async with scoped_session(maker, ctx.session) as session:
            row = await repo.insert(
                session,
                blob_sha256=sha,
                kind="edit",
                model=_EDIT_MODEL,
                prompt=prompt,
                source_sha256=source_sha,
                width=width,
                height=height,
                steps=steps,
                seed=seed,
            )
        return ToolOutput(
            f"Edited the image (seed {seed}); the app is showing the result to the owner.",
            view=generated_image_view(row),
        )

    return {"generate_image": generate_image_tool, "edit_image": edit_image_tool}
