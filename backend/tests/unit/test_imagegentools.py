"""Image-gen tool wiring (no DB): the sidecars are dropped when ComfyUI is unconfigured
(graceful degrade), and when present they are jerv-only `web`-class tools — the curator
(the default knowledge agent) is never offered them.

The handler behaviour (insert, view, source resolution, error paths) is covered against real
Postgres in tests/integration/test_imagegentools_pg.py."""

from pathlib import Path
from typing import Any

from jbrain.agent.agents import JERV_TOOLS
from jbrain.agent.readtools import IMAGE_TOOL_NAMES, TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry, load_registry


async def _noop(_arguments: dict, _ctx: Any) -> str:
    return ""


def test_image_sidecars_exist_and_are_web_class() -> None:
    """Both `.tool` sidecars ship and declare the jerv-only `web` permission class
    (the gate that keeps them off the curator) plus the expensive/side-effecting flags."""
    for name in IMAGE_TOOL_NAMES:
        tf = load_tool(TOOLS_DIR / f"{name}.tool")
        assert tf.spec.permission == "web"
        assert tf.spec.cost_class == "expensive"
        assert tf.spec.side_effecting is True
        assert tf.spec.params["required"] == ["prompt"]


def test_analyze_image_sidecar_is_a_web_read_not_side_effecting() -> None:
    """analyze_image is jerv-only (`web`) like the gen tools, but a READ: it produces no
    stored image, so it is not side-effecting and not in the side-effecting IMAGE_TOOL_NAMES
    — it rides with them in the optional set (dropped together when ComfyUI is unconfigured)."""
    from jbrain.agent.readtools import OPTIONAL_IMAGE_TOOLS

    tf = load_tool(TOOLS_DIR / "analyze_image.tool")
    assert tf.spec.permission == "web"
    assert tf.spec.side_effecting is False
    assert tf.spec.params["required"] == ["prompt"]
    assert "analyze_image" not in IMAGE_TOOL_NAMES
    assert "analyze_image" in OPTIONAL_IMAGE_TOOLS


def test_optional_sidecars_dropped_when_unconfigured(tmp_path: Path) -> None:
    """`load_registry(optional=...)` drops an optional sidecar that has no handler (ComfyUI
    unset) rather than failing — so the registry never advertises an unbacked tool."""
    (tmp_path / "generate_image.tool").write_text(
        (TOOLS_DIR / "generate_image.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "search.tool").write_text(
        (TOOLS_DIR / "search.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry = load_registry(tmp_path, {"search": _noop}, optional=IMAGE_TOOL_NAMES)
    assert "search" in registry
    assert "generate_image" not in registry  # optional + no handler → dropped


def test_optional_sidecar_with_handler_is_kept(tmp_path: Path) -> None:
    """An optional sidecar WITH a handler (ComfyUI configured) still binds normally."""
    (tmp_path / "edit_image.tool").write_text(
        (TOOLS_DIR / "edit_image.tool").read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry = load_registry(tmp_path, {"edit_image": _noop}, optional=IMAGE_TOOL_NAMES)
    assert "edit_image" in registry


def _image_registry() -> ToolRegistry:
    """A registry of just the two image sidecars, bound to no-op handlers — enough to assert
    the permission gate (curator vs jerv) without composing every other tool."""
    return ToolRegistry(
        [
            RegisteredTool(toolfile=load_tool(TOOLS_DIR / f"{name}.tool"), handler=_noop)
            for name in IMAGE_TOOL_NAMES
        ]
    )


def test_image_tools_are_jerv_only_not_curator() -> None:
    """The `web` class is opt-in: the curator (allow=None) is offered neither tool, while
    jerv (allowlisting them via JERV_TOOLS) is offered both."""
    registry = _image_registry()

    curator_offered = registry.allowed_names(scopes=("general", "health", "finance", "location"))
    assert "generate_image" not in curator_offered
    assert "edit_image" not in curator_offered

    jerv_offered = registry.allowed_names(scopes=(), allow=JERV_TOOLS)
    assert {"generate_image", "edit_image"} <= jerv_offered


def test_progress_callback_data_uris_previews_and_passes_steps() -> None:
    """The driver's (step, total, preview_bytes) ticks reach the turn sink with the
    preview base-64'd into a data URI; a missing preview passes through as None."""
    from jbrain.agent.imagegentools import _progress_callback
    from jbrain.agent.loop import ToolContext
    from jbrain.db.session import SessionContext

    seen: list[tuple[int, int, str | None]] = []
    ctx = ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        emit_progress=lambda s, t, p: seen.append((s, t, p)),
    )
    cb = _progress_callback(ctx)
    assert cb is not None
    cb(5, 20, b"jpegbytes")
    cb(20, 20, None)
    assert seen[0] == (5, 20, "data:image/jpeg;base64,anBlZ2J5dGVz")
    assert seen[1] == (20, 20, None)


def test_progress_callback_is_none_without_a_sink() -> None:
    # The batch path (no emit_progress) yields no callback, so the driver skips its
    # WebSocket and just polls for the final image.
    from jbrain.agent.imagegentools import _progress_callback
    from jbrain.agent.loop import ToolContext
    from jbrain.db.session import SessionContext

    ctx = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    assert _progress_callback(ctx) is None


def test_dims_scale_with_resolution_and_stay_multiples_of_64() -> None:
    """aspect sets the ratio, resolution the size; medium is the 1024 default and the
    three presets all land on the multiples of 64 Qwen's latent grid expects."""
    from jbrain.agent.imagegentools import _dims

    assert _dims("square", "medium") == (1024, 1024)  # the default size
    assert _dims(None, None) == (1024, 1024)  # square + medium are the fallbacks
    assert _dims("portrait", "small") == (576, 768)
    assert _dims("landscape", "large") == (1280, 960)
    for aspect in ("square", "portrait", "landscape"):
        for resolution in ("small", "medium", "large"):
            w, h = _dims(aspect, resolution)  # type: ignore[misc]
            assert w % 64 == 0 and h % 64 == 0


def test_dims_reject_unknown_aspect_or_resolution() -> None:
    # A bad value in either axis is a clean None the handler turns into a tool error.
    from jbrain.agent.imagegentools import _dims

    assert _dims("hexagon", "medium") is None
    assert _dims("square", "gigantic") is None


def test_megapixels_track_resolution_for_the_edit_path() -> None:
    """The edit graph scales the source to a total-pixel budget; medium keeps the
    graph's authored 1.6 MP, small/large step it down/up."""
    from jbrain.agent.imagegentools import _megapixels

    assert _megapixels("medium") == 1.6
    assert _megapixels(None) == 1.6  # medium is the fallback
    assert _megapixels("small") < _megapixels("large")


async def test_free_local_llms_unloads_every_resident_model() -> None:
    """Before a render, the image tool frees the unified-memory the LLM holds."""
    from jbrain.agent.imagegentools import _free_local_llms
    from tests.unit.fakes import FakeLocalGateway

    gw = FakeLocalGateway(running={"qwen3-vl-30b-a3b", "gpt-oss-120b"})
    await _free_local_llms(gw)
    assert set(gw.unloaded) == {"qwen3-vl-30b-a3b", "gpt-oss-120b"}


async def test_free_local_llms_is_a_noop_when_nothing_is_loaded() -> None:
    # A cloud-driven turn (or hosting off) has nothing resident — no unloads.
    from jbrain.agent.imagegentools import _free_local_llms
    from tests.unit.fakes import FakeLocalGateway

    gw = FakeLocalGateway(running=set())
    await _free_local_llms(gw)
    assert gw.unloaded == []


async def test_free_local_llms_swallows_a_gateway_failure() -> None:
    # Memory housekeeping must never fail the generation — a gateway error is logged.
    from jbrain.agent.imagegentools import _free_local_llms
    from tests.unit.fakes import FakeLocalGateway

    gw = FakeLocalGateway(running={"gpt-oss-120b"}, fail_unload=True)
    await _free_local_llms(gw)  # does not raise


def test_is_uuid_accepts_real_ids_and_rejects_a_guessed_one() -> None:
    """Source ids are uuid PKs; a non-uuid (a model guessing "latest") is rejected so the
    lookup never hands the DB a bad argument and leaks a raw error to the model."""
    from jbrain.agent.imagegentools import _is_uuid

    assert _is_uuid("852c8203-6742-481a-b284-2771037d8916") is True
    assert _is_uuid("latest") is False
    assert _is_uuid("") is False
    assert _is_uuid("x") is False


def test_png_dims_reads_the_ihdr_and_rejects_non_png() -> None:
    """The recorded output size comes from the PNG's IHDR (an edit's source-scaled
    output differs from the requested preset); a non-PNG falls through to None."""
    from jbrain.agent.imagegentools import _png_dims
    from jbrain.image_gen.fake import _png_with_dims

    assert _png_dims(_png_with_dims(1264, 948)) == (1264, 948)
    assert _png_dims(b"not a png at all, just bytes") is None
    assert _png_dims(b"\x89PNG\r\n\x1a\n") is None  # signature only, no IHDR dims


async def test_free_comfyui_model_unloads_and_frees() -> None:
    """After a render the tool unloads ComfyUI's model and frees its memory."""
    from jbrain.agent.imagegentools import _free_comfyui_model
    from tests.unit.fakes import FakeComfyUiGateway

    gw = FakeComfyUiGateway()
    await _free_comfyui_model(gw)
    assert gw.frees == [(True, True)]


async def test_free_comfyui_model_swallows_a_gateway_failure() -> None:
    # The image is already in hand, so a free() failure is logged, never fatal.
    from jbrain.agent.imagegentools import _free_comfyui_model
    from tests.unit.fakes import FakeComfyUiGateway

    await _free_comfyui_model(FakeComfyUiGateway(fail_free=True))  # does not raise
