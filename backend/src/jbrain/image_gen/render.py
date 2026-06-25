"""The shared image-render core (Wave L2): the resolution/seed/steps/speed→model logic,
the unified-memory time-share (free the LLM → render → free ComfyUI), the blob put, and the
RLS-scoped `generated_images` insert — extracted from `agent/imagegentools.py` so the jerv
handlers AND the direct owner API (`api/images_render.py`) drive ONE render path and never
diverge (docs/IMAGE_LAUNCHER_PLAN.md, Wave L2).

The service takes an explicit `SessionContext` (the value the handlers pass as `ctx.session`
to `scoped_session`), not a `ToolContext`: each caller owns source resolution and supplies its
own RLS scope. Validation/not-installed surface as typed exceptions (`RenderValidationError`,
`ModelNotInstalledError`) the caller maps to its own surface — the jerv handler to model-facing
error strings, the API to HTTP — while `ImageGenError`/`ImageGenInterrupted` propagate untouched.

All LLM unloading rides the local gateway (no LLM call here — image gen isn't an LLM call,
CLAUDE.md rule 1 n/a), all bytes go through the `BlobStore` (rule 2), and the insert runs on a
caller-scoped session under the owner-only firewall (rule 3)."""

from __future__ import annotations

import secrets
from collections.abc import Sequence

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.image_gen.comfyui import EditSpec, GenSpec, ImageGen, OnProgress
from jbrain.image_gen.gateway import ComfyUiGatewayError, ComfyUiMemory
from jbrain.llm.local_gateway import LocalGateway, LocalGatewayError
from jbrain.models.images import GeneratedImage, GeneratedImageRepo
from jbrain.storage import BlobStore

log = structlog.get_logger()

# The models behind the `speed` knob. Generate has three tiers — `quality` (the full Qwen
# model, default), `fast` (its 4-step Lightning sibling), and `dreamshaper` (a tiny SDXL
# checkpoint that renders in seconds). Edit has only `fast`/`quality` (DreamShaper can't edit).
# The recorded `model` string is the graph key the driver routes on; the non-quality ids double
# as the provisioned-catalog ids each tier is gated on (they coincide, so one id serves both).
_GEN_MODEL = "qwen-image-2512"
_FAST_MODEL = "qwen-image-lightning"
_DREAMSHAPER_MODEL = "dreamshaper"
_EDIT_MODEL = "qwen-image-edit"
_FAST_EDIT_MODEL = "qwen-image-edit-lightning"

# The quality path takes a direct `steps` count in the 20–40 band (default 20 — the band's
# floor, a quick-but-finished render; raise toward 40 for more detail at more time). The `fast`
# path ignores steps entirely: the distilled Lightning schedule is a fixed 4 steps (its sweet
# spot — more steps add time, not detail). DreamShaper is likewise fixed at its sweet spot.
_FAST_STEPS = 4
_DREAMSHAPER_STEPS = 6  # DreamShaper XL Lightning's sweet spot in its tiny 4–8 band; not tunable

# Generate's speed tiers -> (recorded model id, fixed step count or None to use the quality band).
# The non-quality tiers also carry the human label + setup id for the not-installed message.
_GEN_SPEEDS: dict[str, tuple[str, int | None]] = {
    "dreamshaper": (_DREAMSHAPER_MODEL, _DREAMSHAPER_STEPS),
    "fast": (_FAST_MODEL, _FAST_STEPS),
    "quality": (_GEN_MODEL, None),
}
_GEN_SPEED_INSTALL_HINT: dict[str, tuple[str, str]] = {
    "dreamshaper": ("DreamShaper XL (SDXL Lightning)", "dreamshaper"),
    "fast": ("Qwen-Image 4-step Lightning", "qwen-image-lightning"),
}
_QUALITY_MIN_STEPS = 20
_QUALITY_MAX_STEPS = 40
_DEFAULT_QUALITY_STEPS = _QUALITY_MIN_STEPS

# A bigint seed: positive and within the model's accepted range (random when absent).
_SEED_BITS = 63

# aspect → (width_frac, height_frac) of a resolution's edge (the LONG side is always the
# full edge). square is 1:1; portrait/landscape are a gentle 3:4; wide/tall are a dramatic
# 16:9 (cinematic / phone-tall). Scaled by the edge below and snapped to multiples of 64.
_ASPECTS: dict[str, tuple[float, float]] = {
    "square": (1.0, 1.0),
    "portrait": (0.75, 1.0),
    "landscape": (1.0, 0.75),
    "tall": (0.5625, 1.0),
    "wide": (1.0, 0.5625),
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

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class RenderValidationError(Exception):
    """A request the service refuses BEFORE any spend — a bad aspect/resolution. The message is
    the model-facing/HTTP-facing reason; each caller maps it to its surface (string / 400)."""


class ModelNotInstalledError(Exception):
    """A non-quality speed tier whose model the operator never provisioned. The message names
    the tier and the `comfyui-setup.sh` command to install it — actionable, not opaque."""


def _snap64(value: float) -> int:
    """Round to the nearest multiple of 64 (>=64) — the latent grid Qwen expects."""
    return max(64, round(value / 64) * 64)


def _dims(aspect: str | None, resolution: str | None) -> tuple[int, int] | None:
    """The (w, h) in px for an aspect at a resolution, snapped to multiples of 64;
    None when either the aspect or the resolution is unrecognized (the caller errors)."""
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


def _resolve_steps(arguments: dict, *, fast: bool = False) -> int:
    """The step count for a request. The `fast` (Lightning) path is a FIXED 4 steps — its
    distilled schedule isn't tunable, so `steps` is ignored there. The quality path takes the
    `steps` argument clamped into the 20–40 band, defaulting to 20 when absent or nonsensical."""
    if fast:
        return _FAST_STEPS
    raw_steps = arguments.get("steps")
    if isinstance(raw_steps, int) and not isinstance(raw_steps, bool) and raw_steps > 0:
        return max(_QUALITY_MIN_STEPS, min(_QUALITY_MAX_STEPS, raw_steps))
    return _DEFAULT_QUALITY_STEPS


def _resolve_fast(raw: object) -> bool:
    """Whether the `fast` (4-step Lightning) model was asked for. Only the exact "fast" opts
    in; anything else (absent, "quality", or a hallucinated value) is the quality default, so
    an unknown speed never silently degrades the image. Used by the edit path (fast/quality)."""
    return isinstance(raw, str) and raw.strip().lower() == "fast"


def _resolve_gen_speed(raw: object) -> str:
    """The generate speed tier: 'dreamshaper' | 'fast' | 'quality'. Only an exact
    (case-insensitive) match opts into a non-default tier; anything else stays 'quality', so an
    unknown/hallucinated speed never silently degrades the render."""
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _GEN_SPEEDS:
            return token
    return "quality"


def _png_dims(data: bytes) -> tuple[int, int] | None:
    """The (width, height) from a PNG's IHDR — the two big-endian uint32 at offset 16
    — or None if it isn't a PNG we can read. Used to record the ACTUAL output size: an
    edit scales the source to a megapixel budget preserving its aspect, so the rendered
    image's dims differ from the requested aspect preset; recording the real dims keeps
    the card's aspect (and meta) honest, no letterbox band in the compare frame."""
    if len(data) < 24 or data[:8] != _PNG_SIGNATURE:
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height) if width > 0 and height > 0 else None


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


async def _free_comfyui_model(gateway: ComfyUiMemory) -> None:
    """Time-share the other direction: after a render, unload ComfyUI's resident
    diffusion model so the ~39 GB it pins returns to the unified pool. ComfyUI caches
    the model for the next render by default, but on this box that 39 GB blocks the
    reply's LLM reload, a follow-up edit (a *different* ~38 GB model), or switching
    back to a large local model (gpt-oss-120b) — so we reclaim it now. The cost is a
    cold model-load on the next image; the memory headroom is worth it here.

    Best-effort: the image is already in hand, so a gateway hiccup is logged, never
    fatal."""
    try:
        await gateway.free(unload_models=True, free_memory=True)
    except ComfyUiGatewayError as exc:
        log.info("image_gen.comfyui_free_skipped", error=str(exc))


class ImageRenderService:
    """The shared generate/edit core. Constructed once (beside `build_image_handlers`, on the
    same ComfyUI gate); both the jerv handlers and the owner API call it.

    `maker` opens the RLS-scoped transaction each insert runs under (the repo takes an
    already-scoped `AsyncSession`); the firewall is Postgres', applied from the caller's
    `SessionContext`. `provisioned_models` is the catalog ids the operator downloaded — the
    non-quality speed tiers are gated on it so an uninstalled model fails actionably before
    any spend, not with ComfyUI's opaque missing-checkpoint error."""

    def __init__(
        self,
        imagegen: ImageGen,
        blob_store: BlobStore,
        repo: GeneratedImageRepo,
        maker: async_sessionmaker[AsyncSession],
        local_gateway: LocalGateway,
        comfyui_gateway: ComfyUiMemory,
        provisioned_models: Sequence[str] = (),
    ) -> None:
        self._imagegen = imagegen
        self._blob_store = blob_store
        self._repo = repo
        self._maker = maker
        self._local_gateway = local_gateway
        self._comfyui_gateway = comfyui_gateway
        self._installed = set(provisioned_models)

    async def generate(
        self,
        ctx: SessionContext,
        *,
        prompt: str,
        aspect: str | None,
        resolution: str | None,
        steps: object = None,
        seed: object = None,
        speed: object = None,
        negative_prompt: str = "",
        on_progress: OnProgress | None = None,
    ) -> GeneratedImage:
        """Text→image: resolve dims/seed/steps/speed (raising the typed errors), free the LLM,
        drive the image model, free ComfyUI, store the PNG, and record one immutable row under
        the caller's RLS scope. Returns the inserted `GeneratedImage` (or raises)."""
        dims = self._resolve_dims(aspect, resolution)
        width, height = dims
        resolved_seed = _resolve_seed(seed)
        tier = _resolve_gen_speed(speed)
        model, fixed_steps = _GEN_SPEEDS[tier]
        if tier != "quality" and model not in self._installed:
            # A non-quality tier is always offered, but its model is a separate install —
            # say so plainly (and how to fix it) rather than letting ComfyUI 404 the checkpoint.
            label, setup_id = _GEN_SPEED_INSTALL_HINT[tier]
            raise ModelNotInstalledError(
                f"The {tier} image model ({label}) isn't installed on this box yet. Generate at "
                f"the default quality instead, or ask the owner to run `comfyui-setup.sh "
                f"{setup_id}`."
            )
        # quality reads the `steps` band; the fixed tiers (fast/dreamshaper) ignore it.
        resolved_steps = (
            fixed_steps if fixed_steps is not None else _resolve_steps({"steps": steps})
        )
        spec = GenSpec(
            prompt=prompt,
            width=width,
            height=height,
            steps=resolved_steps,
            seed=resolved_seed,
            model=model,
            negative_prompt=negative_prompt.strip(),
        )
        await _free_local_llms(self._local_gateway)
        # An interrupt/error propagates here untouched (skipping the ComfyUI free + the row),
        # exactly as the original handler did — the caller maps it to its own surface.
        png = await self._imagegen.generate(spec, on_progress)
        await _free_comfyui_model(self._comfyui_gateway)
        out_w, out_h = _png_dims(png) or (width, height)
        return await self._store(
            ctx,
            png=png,
            kind="generate",
            model=model,
            prompt=prompt,
            source_sha256=None,
            width=out_w,
            height=out_h,
            steps=resolved_steps,
            seed=resolved_seed,
        )

    async def edit(
        self,
        ctx: SessionContext,
        *,
        prompt: str,
        source_bytes: bytes,
        source_sha: str,
        resolution: str | None,
        aspect: str | None = None,
        extra_sources: Sequence[bytes] = (),
        steps: object = None,
        seed: object = None,
        speed: object = None,
        negative_prompt: str = "",
        on_progress: OnProgress | None = None,
    ) -> GeneratedImage:
        """Image→image: the primary source bytes (+sha) and any extra references are ALREADY
        resolved by the caller. Resolves dims/seed/steps/speed (raising the typed errors on a
        bad preset or an uninstalled fast model), runs the unload→render→unload→store sequence,
        and records the edit row (carrying the source sha). Returns the inserted row (or raises).

        Reference-count validation (the MAX_EDIT_IMAGES cap) stays with each caller, since each
        resolves its own references; the service trusts the list it's handed."""
        dims = self._resolve_dims(aspect, resolution)
        width, height = dims
        resolved_seed = _resolve_seed(seed)
        fast = _resolve_fast(speed)
        if fast and _FAST_EDIT_MODEL not in self._installed:
            # Same actionable bail as generate: the fast edit model is a separate install.
            raise ModelNotInstalledError(
                "The fast image model (Qwen-Image-Edit 4-step Lightning) isn't installed on "
                "this box yet. Edit at the default quality instead, or ask the owner to run "
                "`comfyui-setup.sh qwen-image-edit-lightning`."
            )
        model = _FAST_EDIT_MODEL if fast else _EDIT_MODEL
        resolved_steps = _resolve_steps({"steps": steps}, fast=fast)
        spec = EditSpec(
            prompt=prompt,
            width=width,
            height=height,
            steps=resolved_steps,
            seed=resolved_seed,
            model=model,
            megapixels=_megapixels(resolution),
            negative_prompt=negative_prompt.strip(),
        )
        await _free_local_llms(self._local_gateway)
        png = await self._imagegen.edit(
            spec, source_bytes, on_progress, extra_sources=list(extra_sources)
        )
        await _free_comfyui_model(self._comfyui_gateway)
        # The edit scales the source to a megapixel budget preserving ITS aspect, so the output
        # dims differ from the requested preset — record the real ones (the row's dims drive the
        # card's aspect; a mismatch letterboxes the before/after frame).
        out_w, out_h = _png_dims(png) or (width, height)
        return await self._store(
            ctx,
            png=png,
            kind="edit",
            model=model,
            prompt=prompt,
            source_sha256=source_sha,
            width=out_w,
            height=out_h,
            steps=resolved_steps,
            seed=resolved_seed,
        )

    @staticmethod
    def _resolve_dims(aspect: str | None, resolution: str | None) -> tuple[int, int]:
        """The validated (w, h), or a `RenderValidationError` naming the offending axis — the
        single place generate/edit reject a bad preset before any spend."""
        dims = _dims(aspect, resolution)
        if dims is None:
            if (aspect or _DEFAULT_ASPECT) not in _ASPECTS:
                raise RenderValidationError(
                    "aspect must be one of: square, portrait, landscape, tall, wide."
                )
            raise RenderValidationError("resolution must be one of: small, medium, large.")
        return dims

    async def _store(
        self,
        ctx: SessionContext,
        *,
        png: bytes,
        kind: str,
        model: str,
        prompt: str,
        source_sha256: str | None,
        width: int,
        height: int,
        steps: int,
        seed: int,
    ) -> GeneratedImage:
        """Put the result PNG (rule 2) and insert one immutable row on the caller's RLS-scoped
        session (rule 3). Returns the inserted row."""
        sha = await self._blob_store.put(png)
        async with scoped_session(self._maker, ctx) as session:
            return await self._repo.insert(
                session,
                blob_sha256=sha,
                kind=kind,
                model=model,
                prompt=prompt,
                source_sha256=source_sha256,
                width=width,
                height=height,
                steps=steps,
                seed=seed,
            )
