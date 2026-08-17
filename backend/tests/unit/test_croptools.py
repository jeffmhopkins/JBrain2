"""The crop lane (jbrain.agent.croptools) — AGENT_CANVAS_PLAN W4.

The contract that matters most here is honesty about what was NOT found. Region-finding
in an image fails silently — six boxes returned for fourteen people looks exactly like a
complete answer — so the result must state the count and refuse to imply completeness.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from PIL import Image

from jbrain.agent.croptools import MAX_CROPS, MIN_CROP_PX, PROVENANCE_CROP, build_crop_handlers
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext


@dataclass
class _Row:
    id: str = "img1"
    provenance: str = ""


class _Router:
    def __init__(self, text: str = "[]", model: str = "qwen3.8-27b") -> None:
        self.text = text
        self.model = model
        self.calls: list[dict] = []

    async def supports_vision(self, task: str, spec_override: str | None = None) -> bool:
        return True

    async def effective_spec(
        self, task: str, strength: str | None = None, spec_override: str | None = None
    ):
        # Mirrors the real router: an override is a "provider:model" selector and the
        # BARE model comes back. A fake that ignored spec_override would have hidden the
        # bug where the selector was used as a table key.
        if spec_override:
            return tuple(spec_override.split(":", 1))  # type: ignore[return-value]
        return "local", self.model

    async def complete(self, task: str, **kw: Any):
        self.calls.append({"task": task, **kw})
        return type("R", (), {"text": self.text})()


@dataclass
class _Ctx:
    session: SessionContext = field(
        default_factory=lambda: SessionContext(principal_id="owner", principal_kind="owner")
    )
    agent_session_id: str | None = "sess-1"
    model_override: str | None = None


def _ctx() -> ToolContext:
    return _Ctx()  # type: ignore[return-value]


def _photo(width: int = 1000, height: int = 800) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 150)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _build(monkeypatch, *, source: bytes | str = b"", router: _Router | None = None):
    persisted: list[dict] = []
    counter = {"n": 0}

    async def _fake_resolve(image_id, attachment_id, **kw):
        if isinstance(source, str):
            return source
        return (source, "sha-src")

    async def _fake_persist(maker, ctx, blobs, repo, *, data, provenance, model, prompt):
        counter["n"] += 1
        persisted.append({"data": data, "provenance": provenance, "prompt": prompt})
        return _Row(id=f"img{counter['n']}", provenance=provenance)

    monkeypatch.setattr("jbrain.agent.croptools.resolve_source", _fake_resolve)
    monkeypatch.setattr("jbrain.agent.croptools.persist_chat_image", _fake_persist)
    # Fakes at the seam — see the note in test_drawtools.
    handlers = build_crop_handlers(*cast(Any, (None, None, None, None, router or _Router())))
    return handlers["crop_regions"], persisted


def _boxes_json(boxes: list[tuple[int, int, int, int]], label: str = "face") -> str:
    return json.dumps(
        [{"bbox_2d": list(b), "label": f"{label} {i + 1}"} for i, b in enumerate(boxes)]
    )


# --- argument validation ----------------------------------------------------


@pytest.mark.asyncio
async def test_needs_exactly_one_source(monkeypatch) -> None:
    crop, _p = _build(monkeypatch, source=_photo())
    assert "exactly one" in await crop({"target": "faces"}, _ctx())
    assert "exactly one" in await crop(
        {"source_attachment_id": "a", "source_image_id": "b", "target": "x"}, _ctx()
    )


@pytest.mark.asyncio
async def test_needs_a_target_or_boxes(monkeypatch) -> None:
    crop, _p = _build(monkeypatch, source=_photo())
    assert "either `target`" in await crop({"source_attachment_id": "a"}, _ctx())


# --- grounding path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_crops_each_grounded_region(monkeypatch) -> None:
    # Normalized 0-1000 boxes against a 1000x800 image.
    router = _Router(_boxes_json([(100, 100, 300, 300), (500, 200, 700, 400)]))
    crop, persisted = _build(monkeypatch, source=_photo(), router=router)
    out = await crop({"source_attachment_id": "att-1", "target": "each face"}, _ctx())
    assert len(persisted) == 2
    assert all(p["provenance"] == PROVENANCE_CROP for p in persisted)
    assert "Cut 2 crop(s)" in out


@pytest.mark.asyncio
async def test_the_result_refuses_to_imply_completeness(monkeypatch) -> None:
    # The single most important line in this tool: an undercount is silent, so the
    # model must be told the number is what was FOUND, not what exists.
    router = _Router(_boxes_json([(100, 100, 300, 300)]))
    crop, _p = _build(monkeypatch, source=_photo(), router=router)
    out = await crop({"source_attachment_id": "att-1", "target": "each face"}, _ctx())
    assert "not necessarily all there are" in out


@pytest.mark.asyncio
async def test_finding_nothing_is_a_clear_answer(monkeypatch) -> None:
    crop, persisted = _build(monkeypatch, source=_photo(), router=_Router("[]"))
    out = await crop({"source_attachment_id": "att-1", "target": "a giraffe"}, _ctx())
    assert "Found nothing matching" in out
    assert persisted == []


@pytest.mark.asyncio
async def test_an_unqualified_model_refuses_rather_than_cropping_wrong(monkeypatch) -> None:
    # A wrong coordinate base cuts confidently wrong crops — worse than no crops.
    router = _Router(_boxes_json([(100, 100, 300, 300)]), model="some-unmeasured-vl")
    crop, persisted = _build(monkeypatch, source=_photo(), router=router)
    out = await crop({"source_attachment_id": "att-1", "target": "faces"}, _ctx())
    assert "Can't crop with the current vision model" in out
    assert persisted == []


@pytest.mark.asyncio
async def test_a_vision_failure_is_recoverable(monkeypatch) -> None:
    class _Boom(_Router):
        async def complete(self, task: str, **kw: Any):
            from jbrain.llm.errors import LlmError

            raise LlmError("vision unreachable")

    crop, persisted = _build(monkeypatch, source=_photo(), router=_Boom())
    out = await crop({"source_attachment_id": "att-1", "target": "faces"}, _ctx())
    assert "Couldn't look at that image" in out
    assert persisted == []


# --- explicit boxes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_pixel_boxes_skip_grounding(monkeypatch) -> None:
    router = _Router()
    crop, persisted = _build(monkeypatch, source=_photo(), router=router)
    out = await crop(
        {
            "source_attachment_id": "att-1",
            "boxes": [{"x": 100, "y": 100, "w": 200, "h": 200, "label": "heater"}],
        },
        _ctx(),
    )
    assert router.calls == []  # no vision round needed
    assert len(persisted) == 1
    assert "Cut 1 crop(s)" in out


@pytest.mark.asyncio
async def test_malformed_boxes_are_skipped_with_a_note(monkeypatch) -> None:
    crop, persisted = _build(monkeypatch, source=_photo())
    out = await crop(
        {
            "source_attachment_id": "att-1",
            "boxes": [
                {"x": 100, "y": 100, "w": 200, "h": 200},
                {"x": "left", "y": 1, "w": 2, "h": 3},
                {"x": 5000, "y": 5000, "w": 100, "h": 100},  # entirely off-image
            ],
        },
        _ctx(),
    )
    assert len(persisted) == 1
    assert "2 box(es) were malformed or off-image" in out


# --- bounds -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_cap_truncates_with_a_note_rather_than_erroring(monkeypatch) -> None:
    many = [(i * 10, 0, i * 10 + 80, 200) for i in range(MAX_CROPS + 4)]
    crop, persisted = _build(monkeypatch, source=_photo(), router=_Router(_boxes_json(many)))
    out = await crop({"source_attachment_id": "att-1", "target": "faces"}, _ctx())
    assert len(persisted) == MAX_CROPS
    assert f"only the first {MAX_CROPS}" in out


@pytest.mark.asyncio
async def test_slivers_are_skipped_as_misdetections(monkeypatch) -> None:
    # A few pixels wide is a mis-grounded sliver, not a face.
    router = _Router(_boxes_json([(100, 100, 300, 300), (500, 500, 502, 502)]))
    crop, persisted = _build(monkeypatch, source=_photo(), router=router)
    out = await crop({"source_attachment_id": "att-1", "target": "faces"}, _ctx())
    assert len(persisted) == 1
    assert "too small to crop" in out
    assert MIN_CROP_PX > 2


@pytest.mark.asyncio
async def test_all_slivers_means_nothing_was_cut(monkeypatch) -> None:
    router = _Router(_boxes_json([(500, 500, 502, 502)]))
    crop, persisted = _build(monkeypatch, source=_photo(), router=router)
    out = await crop({"source_attachment_id": "att-1", "target": "faces"}, _ctx())
    assert persisted == []
    assert "too small to be a usable crop" in out


# --- the view payload -------------------------------------------------------


@pytest.mark.asyncio
async def test_one_view_carries_every_crop(monkeypatch) -> None:
    # N separate view events would render N cards live and ONE after reload, so the
    # whole set has to ride a single view.
    router = _Router(_boxes_json([(100, 100, 300, 300), (500, 200, 700, 400)]))
    crop, _p = _build(monkeypatch, source=_photo(), router=router)
    out = await crop({"source_attachment_id": "att-1", "target": "each face"}, _ctx())
    view = out.view  # type: ignore[attr-defined]
    assert view.view == "image_set"
    assert len(view.data["crops"]) == 2
    assert view.data["source_kind"] == "attachment"
    assert view.data["source_id"] == "att-1"


@pytest.mark.asyncio
async def test_boxes_are_fractions_so_the_card_can_overlay_them(monkeypatch) -> None:
    # Variant B draws each region on the source at whatever size it renders, so the
    # payload must be resolution-independent.
    router = _Router(_boxes_json([(100, 250, 300, 500)]))
    crop, _p = _build(monkeypatch, source=_photo(1000, 800), router=router)
    out = await crop({"source_attachment_id": "att-1", "target": "face"}, _ctx())
    box = out.view.data["crops"][0]["box"]  # type: ignore[attr-defined]
    assert box == pytest.approx([0.1, 0.25, 0.3, 0.5])
    assert all(0.0 <= v <= 1.0 for v in box)


@pytest.mark.asyncio
async def test_each_crop_carries_where_its_box_came_from(monkeypatch) -> None:
    # The card shows detected-vs-guessed; without it a wrong crop looks authoritative.
    router = _Router(_boxes_json([(100, 100, 300, 300)]))
    crop, _p = _build(monkeypatch, source=_photo(), router=router)
    grounded = await crop({"source_attachment_id": "a", "target": "face"}, _ctx())
    assert grounded.view.data["crops"][0]["origin"] == "vlm"  # type: ignore[attr-defined]

    crop2, _p2 = _build(monkeypatch, source=_photo())
    explicit = await crop2(
        {"source_attachment_id": "a", "boxes": [{"x": 10, "y": 10, "w": 100, "h": 100}]}, _ctx()
    )
    assert explicit.view.data["crops"][0]["origin"] == "owner"  # type: ignore[attr-defined]


# --- faces go to the detector, and a fallback is stated ---------------------


class _Ocr:
    """Stands in for the RapidOcrClient the face detector rides on."""

    def __init__(self, faces=(), boom: str = "") -> None:
        self.faces = faces
        self.boom = boom
        self.calls = 0


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["each face", "every person", "everyone in the photo"])
async def test_face_targets_use_the_deterministic_detector(monkeypatch, target: str) -> None:
    from jbrain.vision import FaceBox

    router = _Router(_boxes_json([(0, 0, 100, 100)]))  # would be used if we fell back
    ocr = _Ocr(faces=(FaceBox(10, 10, 120, 130, 0.99), FaceBox(200, 40, 300, 160, 0.95)))

    async def _fake_detect(client, data, *, min_score=0.6):
        client.calls += 1
        return client.faces

    monkeypatch.setattr("jbrain.agent.croptools.detect_faces", _fake_detect)
    crop, persisted = _build(monkeypatch, source=_photo(), router=router)
    handlers = build_crop_handlers(*cast(Any, (None, None, None, None, router, ocr)))
    out = await handlers["crop_regions"]({"source_attachment_id": "a", "target": target}, _ctx())

    assert ocr.calls == 1
    assert router.calls == []  # the vision model was never asked
    assert out.view.data["crops"][0]["origin"] == "detector"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_non_face_target_still_uses_grounding(monkeypatch) -> None:
    # Routing "every product label" to a face detector would return faces for a
    # question about labels — so the match is deliberately narrow.
    from jbrain.vision import FaceBox

    router = _Router(_boxes_json([(100, 100, 300, 300)]))
    ocr = _Ocr(faces=(FaceBox(1, 1, 2, 2, 0.9),))

    async def _fake_detect(client, data, *, min_score=0.6):  # pragma: no cover - must not run
        raise AssertionError("a label target must not hit the face detector")

    monkeypatch.setattr("jbrain.agent.croptools.detect_faces", _fake_detect)
    _crop, _p = _build(monkeypatch, source=_photo(), router=router)
    handlers = build_crop_handlers(*cast(Any, (None, None, None, None, router, ocr)))
    out = await handlers["crop_regions"](
        {"source_attachment_id": "a", "target": "every product label"}, _ctx()
    )
    assert out.view.data["crops"][0]["origin"] == "vlm"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_an_unavailable_detector_falls_back_and_SAYS_it_fell_back(monkeypatch) -> None:
    # The whole point of the detector is that the model undercounts silently. Degrading
    # to the model without saying so would reintroduce exactly that failure.
    from jbrain.vision import FaceDetectUnavailable

    router = _Router(_boxes_json([(100, 100, 300, 300)]))

    async def _fake_detect(client, data, *, min_score=0.6):
        raise FaceDetectUnavailable("no YuNet model on this box")

    monkeypatch.setattr("jbrain.agent.croptools.detect_faces", _fake_detect)
    _crop, _p = _build(monkeypatch, source=_photo(), router=router)
    handlers = build_crop_handlers(*cast(Any, (None, None, None, None, router, _Ocr())))
    out = await handlers["crop_regions"]({"source_attachment_id": "a", "target": "faces"}, _ctx())

    assert "face detector was unavailable" in out
    assert "can miss people" in out
    assert out.view.data["crops"][0]["origin"] == "vlm"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_no_ocr_client_configured_falls_back_quietly_to_grounding(monkeypatch) -> None:
    router = _Router(_boxes_json([(100, 100, 300, 300)]))
    crop, _p = _build(monkeypatch, source=_photo(), router=router)  # built with ocr=None
    out = await crop({"source_attachment_id": "a", "target": "faces"}, _ctx())
    assert out.view.data["crops"][0]["origin"] == "vlm"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_conversation_model_override_still_crops(monkeypatch) -> None:
    # Regression: `vision_read_spec` returns a "provider:model" SELECTOR, while the
    # grounding table is keyed on the bare served model. Using the selector as the key
    # refused every crop made from a conversation with a model pick — i.e. every canvas
    # conversation, since the pick is what arms the tools in the first place.
    router = _Router(_boxes_json([(100, 100, 300, 300)]))
    crop, persisted = _build(monkeypatch, source=_photo(), router=router)

    async def _spec(_router, override):
        return "local:qwen3.8-27b-q4"

    monkeypatch.setattr("jbrain.agent.croptools.vision_read_spec", _spec)
    out = await crop({"source_attachment_id": "a", "target": "each bottle"}, _ctx())
    assert "Can't crop with the current vision model" not in out
    assert len(persisted) == 1
