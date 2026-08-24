"""analyze_image tool wiring (no DB): the sidecar is dropped when ComfyUI is unconfigured
(graceful degrade), and when present it is a jerv-only `web`-class READ — the curator
(the default knowledge agent) is never offered it. Image GENERATION/editing is not an
agent tool (the Images launcher owns ComfyUI; its render core is covered in
tests/unit/test_image_render.py, and the render helpers are pinned below against
`jbrain.image_gen.render`).

The handler behaviour (vision read, source resolution, error paths) is covered against real
Postgres in tests/integration/test_imagegentools_pg.py."""

from pathlib import Path
from typing import Any, cast

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.readtools import OPTIONAL_IMAGE_TOOLS, TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry, load_registry
from jbrain.llm import LlmRouter


async def _noop(_arguments: dict, _ctx: Any) -> str:
    return ""


class _VisionRouter:
    """A stand-in router that only answers supports_vision — records what it was asked."""

    def __init__(self, vision: bool) -> None:
        self._vision = vision
        self.checked: list[tuple[str, str | None]] = []

    async def supports_vision(self, task: str, spec_override: str | None = None) -> bool:
        self.checked.append((task, spec_override))
        return self._vision


async def test_vision_read_reuses_a_vision_capable_pick_else_the_default_route() -> None:
    """analyze_image's vision read reuses the conversation's own model ONLY when that pick can
    see (no residency swap); a text-only pick or no pick falls back to the agent.vision route."""
    from jbrain.agent.chat_images import vision_read_spec

    # A vision-capable omnibox pick → reuse it (the read runs on the resident turn model).
    seeing = cast(LlmRouter, _VisionRouter(vision=True))
    assert await vision_read_spec(seeing, "local:qwen3.8-27b") == "local:qwen3.8-27b"
    assert cast(_VisionRouter, seeing).checked == [("agent.vision", "local:qwen3.8-27b")]

    # A text-only pick can't see → None, so the separate vision route (its point) applies.
    blind = cast(LlmRouter, _VisionRouter(vision=False))
    assert await vision_read_spec(blind, "local:gpt-oss-120b") is None

    # No pick at all → None without even probing (the default route already fits).
    unasked = _VisionRouter(vision=True)
    assert await vision_read_spec(cast(LlmRouter, unasked), None) is None
    assert unasked.checked == []


def test_analyze_image_sidecar_is_a_web_read_not_side_effecting() -> None:
    """analyze_image is jerv-only (`web`) but a READ: it produces no stored image, so it is
    not side-effecting. It is the whole optional image set (dropped when ComfyUI is
    unconfigured) — the gen pair is gone, the launcher owns generation."""
    tf = load_tool(TOOLS_DIR / "analyze_image.tool")
    assert tf.spec.permission == "web"
    assert tf.spec.side_effecting is False
    assert tf.spec.params["required"] == ["prompt"]
    assert frozenset({"analyze_image"}) == OPTIONAL_IMAGE_TOOLS
    # The removed generation pair must not ship as sidecars any more.
    assert not (TOOLS_DIR / "generate_image.tool").exists()
    assert not (TOOLS_DIR / "edit_image.tool").exists()


def test_optional_sidecars_dropped_when_unconfigured(tmp_path: Path) -> None:
    """`load_registry(optional=...)` drops an optional sidecar that has no handler (ComfyUI
    unset) rather than failing — so the registry never advertises an unbacked tool."""
    (tmp_path / "analyze_image.tool").write_text(
        (TOOLS_DIR / "analyze_image.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "search.tool").write_text(
        (TOOLS_DIR / "search.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry = load_registry(tmp_path, {"search": _noop}, optional=OPTIONAL_IMAGE_TOOLS)
    assert "search" in registry
    assert "analyze_image" not in registry  # optional + no handler → dropped


def test_optional_sidecar_with_handler_is_kept(tmp_path: Path) -> None:
    """An optional sidecar WITH a handler (ComfyUI configured) still binds normally."""
    (tmp_path / "analyze_image.tool").write_text(
        (TOOLS_DIR / "analyze_image.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry = load_registry(tmp_path, {"analyze_image": _noop}, optional=OPTIONAL_IMAGE_TOOLS)
    assert "analyze_image" in registry


def test_analyze_image_is_jerv_only_not_curator() -> None:
    """The `web` class is opt-in: the curator (allow=None) is never offered the sidecar,
    while jerv (allowlisting it via JERV_TOOLS) is."""
    registry = ToolRegistry(
        [RegisteredTool(toolfile=load_tool(TOOLS_DIR / "analyze_image.tool"), handler=_noop)]
    )

    curator_offered = registry.allowed_names(scopes=("general", "health", "finance", "location"))
    assert "analyze_image" not in curator_offered

    jerv_offered = registry.allowed_names(scopes=(), allow=JERV_TOOLS)
    assert "analyze_image" in jerv_offered


# --- the launcher's render helpers (jbrain.image_gen.render) ----------------
# These used to be pinned through the agent gen tools; the agent path is gone but the
# Images launcher still drives the same core, so the helper contracts stay pinned here.


def test_resolve_gen_speed_maps_each_tier_to_its_model_and_steps() -> None:
    """The three generate tiers resolve to their model + fixed step count; quality uses the
    band (None), and an unknown/absent speed falls back to quality — never a silent downgrade."""
    from jbrain.image_gen.render import _GEN_SPEEDS, _resolve_gen_speed

    assert _resolve_gen_speed("dreamshaper") == "dreamshaper"
    assert _resolve_gen_speed("FAST") == "fast" and _resolve_gen_speed(" Quality ") == "quality"
    assert _resolve_gen_speed(None) == "quality" and _resolve_gen_speed("turbo") == "quality"
    assert _GEN_SPEEDS["dreamshaper"] == ("dreamshaper", 6)
    assert _GEN_SPEEDS["fast"] == ("qwen-image-lightning", 4)
    assert _GEN_SPEEDS["quality"] == ("qwen-image-2512", None)  # None → the quality steps band


def test_fast_path_is_a_fixed_four_steps() -> None:
    """The fast (Lightning) path is a fixed 4 steps regardless of the `steps` argument — the
    distilled schedule isn't tunable, so the knob can't drift it off its sweet spot."""
    from jbrain.image_gen.render import _FAST_STEPS, _resolve_steps

    assert _FAST_STEPS == 4
    assert _resolve_steps({}, fast=True) == 4
    assert _resolve_steps({"steps": 30}, fast=True) == 4  # an explicit steps is ignored when fast


def test_resolve_fast_only_opts_in_on_exact_fast() -> None:
    """Only "fast" (any case) selects the distilled model; absent/quality/garbage all stay
    on the quality default, so an unknown speed never silently degrades the render."""
    from jbrain.image_gen.render import _resolve_fast, _resolve_steps

    assert _resolve_fast("fast") and _resolve_fast("FAST") and _resolve_fast(" fast ")
    assert not _resolve_fast("quality") and not _resolve_fast(None) and not _resolve_fast("turbo")
    # fast is a fixed 4 steps; quality defaults to the 20-step band floor.
    assert _resolve_steps({}, fast=True) == 4
    assert _resolve_steps({}, fast=False) == 20


def test_dims_scale_with_resolution_and_stay_multiples_of_64() -> None:
    """aspect sets the ratio, resolution the size; medium is the 1024 default and the
    three presets all land on the multiples of 64 Qwen's latent grid expects."""
    from jbrain.image_gen.render import _dims

    assert _dims("square", "medium") == (1024, 1024)  # the default size
    assert _dims(None, None) == (1024, 1024)  # square + medium are the fallbacks
    assert _dims("portrait", "small") == (576, 768)
    assert _dims("landscape", "large") == (1280, 960)
    # 16:9 presets: the long edge is the resolution edge, the short snapped to a /64.
    assert _dims("wide", "medium") == (1024, 576)  # exact 16:9
    assert _dims("tall", "medium") == (576, 1024)
    for aspect in ("square", "portrait", "landscape", "tall", "wide"):
        for resolution in ("small", "medium", "large"):
            w, h = _dims(aspect, resolution)  # type: ignore[misc]
            assert w % 64 == 0 and h % 64 == 0


def test_dims_reject_unknown_aspect_or_resolution() -> None:
    # A bad value in either axis is a clean None the caller turns into a validation error.
    from jbrain.image_gen.render import _dims

    assert _dims("hexagon", "medium") is None
    assert _dims("square", "gigantic") is None


def test_resolve_steps_takes_the_quality_band_and_defaults_to_twenty() -> None:
    """The quality path reads `steps` directly, clamped into the 20–40 band, and defaults to
    the 20-step floor when absent or nonsensical."""
    from jbrain.image_gen.render import _resolve_steps

    assert _resolve_steps({}) == 20  # absent → the band floor / default
    assert _resolve_steps({"steps": "lots"}) == 20  # non-int → default
    assert _resolve_steps({"steps": 33}) == 33  # an in-band value passes through
    assert _resolve_steps({"steps": 40}) == 40  # the band ceiling
    # Out-of-band values are clamped, never escaping 20–40.
    assert _resolve_steps({"steps": 5}) == 20 and _resolve_steps({"steps": 100}) == 40


def test_megapixels_track_resolution_for_the_edit_path() -> None:
    """The edit graph scales the source to a total-pixel budget; medium keeps the
    graph's authored 1.6 MP, small/large step it down/up."""
    from jbrain.image_gen.render import _megapixels

    assert _megapixels("medium") == 1.6
    assert _megapixels(None) == 1.6  # medium is the fallback
    assert _megapixels("small") < _megapixels("large")


async def test_free_local_llms_unloads_every_resident_model() -> None:
    """Before a render, the launcher frees the unified-memory the LLM holds."""
    from jbrain.image_gen.render import _free_local_llms
    from tests.unit.fakes import FakeLocalGateway

    gw = FakeLocalGateway(running={"qwen3-vl-30b-a3b", "gpt-oss-120b"})
    await _free_local_llms(gw)
    assert set(gw.unloaded) == {"qwen3-vl-30b-a3b", "gpt-oss-120b"}


async def test_free_local_llms_is_a_noop_when_nothing_is_loaded() -> None:
    # A cloud-driven box (or hosting off) has nothing resident — no unloads.
    from jbrain.image_gen.render import _free_local_llms
    from tests.unit.fakes import FakeLocalGateway

    gw = FakeLocalGateway(running=set())
    await _free_local_llms(gw)
    assert gw.unloaded == []


async def test_free_local_llms_swallows_a_gateway_failure() -> None:
    # Memory housekeeping must never fail the generation — a gateway error is logged.
    from jbrain.image_gen.render import _free_local_llms
    from tests.unit.fakes import FakeLocalGateway

    gw = FakeLocalGateway(running={"gpt-oss-120b"}, fail_unload=True)
    await _free_local_llms(gw)  # does not raise


def test_is_uuid_accepts_real_ids_and_rejects_a_guessed_one() -> None:
    """Source ids are uuid PKs; a non-uuid (a model guessing "latest") is rejected so the
    lookup never hands the DB a bad argument and leaks a raw error to the model."""
    from jbrain.agent.chat_images import _is_uuid

    assert _is_uuid("852c8203-6742-481a-b284-2771037d8916") is True
    assert _is_uuid("latest") is False
    assert _is_uuid("") is False
    assert _is_uuid("x") is False


def test_png_dims_reads_the_ihdr_and_rejects_non_png() -> None:
    """The recorded output size comes from the PNG's IHDR (an edit's source-scaled
    output differs from the requested preset); a non-PNG falls through to None."""
    from jbrain.image_gen.fake import _png_with_dims
    from jbrain.image_gen.render import _png_dims

    assert _png_dims(_png_with_dims(1264, 948)) == (1264, 948)
    assert _png_dims(b"not a png at all, just bytes") is None
    assert _png_dims(b"\x89PNG\r\n\x1a\n") is None  # signature only, no IHDR dims


async def test_free_comfyui_model_unloads_and_frees() -> None:
    """After a render the launcher unloads ComfyUI's model and frees its memory."""
    from jbrain.image_gen.render import _free_comfyui_model
    from tests.unit.fakes import FakeComfyUiGateway

    gw = FakeComfyUiGateway()
    await _free_comfyui_model(gw)
    assert gw.frees == [(True, True)]


async def test_free_comfyui_model_swallows_a_gateway_failure() -> None:
    # The image is already in hand, so a free() failure is logged, never fatal.
    from jbrain.image_gen.render import _free_comfyui_model
    from tests.unit.fakes import FakeComfyUiGateway

    await _free_comfyui_model(FakeComfyUiGateway(fail_free=True))  # does not raise
