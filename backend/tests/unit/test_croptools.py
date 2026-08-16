"""The crop lane (jbrain.agent.croptools) — AGENT_CANVAS_PLAN W4.

The contract that matters most here is honesty about what was NOT found. Region-finding
in an image fails silently — six boxes returned for fourteen people looks exactly like a
complete answer — so the result must state the count and refuse to imply completeness.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

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

    async def effective_spec(self, task: str, strength: str | None = None):
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
    handlers = build_crop_handlers(None, None, None, None, router or _Router())
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
