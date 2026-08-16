"""The canvas tool handlers (jbrain.agent.drawtools) — AGENT_CANVAS_PLAN W2/W3.

Collaborators are faked (no DB, no LLM, no sidecar) so these test the handler's own
contract: what the model is told, what is persisted, and that the engine ceilings hold
regardless of what the model does.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from PIL import Image

from jbrain.agent.drawtools import (
    ARTIFACT_KIND,
    CALL_BUDGET,
    LOOK_BUDGET,
    PROVENANCE_CANVAS,
    build_canvas_handlers,
)
from jbrain.agent.loop import ToolCallBudget, ToolContext
from jbrain.db.session import SessionContext

# --- fakes ------------------------------------------------------------------


class _Blobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        sha = f"sha{len(self.data)}_{len(data)}"
        self.data[sha] = data
        return sha

    async def get(self, sha: str) -> bytes:
        return self.data[sha]


@dataclass
class _Artifact:
    id: str
    kind: str
    source_url: str
    title: str
    sha256: str


class _Artifacts:
    def __init__(self) -> None:
        self.rows: list[_Artifact] = []
        self.writes = 0

    async def session_context(self, ctx, session_id):
        return SessionContext(principal_id="owner", principal_kind="owner"), "general"

    async def list_for_session(self, ctx, session_id, *, limit):
        return list(reversed(self.rows))

    async def remember(self, ctx, session_id, **kw):
        self.writes += 1
        # Upsert on source_url, the real repo's semantics — a canvas has ONE row.
        for row in self.rows:
            if row.source_url == kw["source_url"]:
                row.sha256 = kw["sha256"]
                row.title = kw["title"]
                return row
        row = _Artifact(
            id=f"a{len(self.rows)}",
            kind=kw["kind"],
            source_url=kw["source_url"],
            title=kw["title"],
            sha256=kw["sha256"],
        )
        self.rows.append(row)
        return row


@dataclass
class _Row:
    id: str = "img1"
    provenance: str = ""
    width: int = 0
    height: int = 0
    kind: str = "generate"
    prompt: str = ""
    model: str = ""
    seed: int = 0
    blob_sha256: str = ""
    source_sha256: str | None = None


class _Images:
    def __init__(self) -> None:
        self.inserted: list[dict] = []


class _Router:
    def __init__(self, text: str = "the box is around the heater") -> None:
        self.text = text
        self.calls: list[dict] = []

    async def supports_vision(self, task: str, spec_override: str | None = None) -> bool:
        return True

    async def complete(self, task: str, **kw: Any):
        self.calls.append({"task": task, **kw})
        return type("R", (), {"text": self.text})()


class _Html:
    configured = False

    async def render(self, *a: Any, **kw: Any) -> bytes:  # pragma: no cover - not reached
        raise AssertionError("no html blocks in these scenes")


@dataclass
class _Ctx:
    session: SessionContext = field(
        default_factory=lambda: SessionContext(principal_id="owner", principal_kind="owner")
    )
    agent_session_id: str | None = "sess-1"
    model_override: str | None = None
    canvas_call_budget: ToolCallBudget | None = None
    canvas_look_budget: ToolCallBudget | None = None


def _photo(width: int = 400, height: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (30, 90, 160)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _build(monkeypatch: pytest.MonkeyPatch, *, source: bytes | str | None = None, router=None):
    blobs, artifacts, images = _Blobs(), _Artifacts(), _Images()
    persisted: list[dict] = []

    async def _fake_resolve(image_id, attachment_id, **kw):
        if isinstance(source, str):
            return source
        return (source, "sha-src")

    async def _fake_persist(maker, ctx, blobs_, repo, *, data, provenance, model, prompt):
        persisted.append({"data": data, "provenance": provenance, "prompt": prompt})
        return _Row(id="img1", provenance=provenance)

    monkeypatch.setattr("jbrain.agent.drawtools.resolve_source", _fake_resolve)
    monkeypatch.setattr("jbrain.agent.drawtools.persist_chat_image", _fake_persist)
    monkeypatch.setattr("jbrain.agent.drawtools.chat_image_view", lambda row: None)

    handlers = build_canvas_handlers(
        None, blobs, artifacts, images, None, router or _Router(), _Html()
    )
    return handlers, blobs, artifacts, persisted


def _ctx(**kw) -> ToolContext:
    return _Ctx(**kw)  # type: ignore[return-value]


def _scene_of(blobs: _Blobs, artifacts: _Artifacts) -> dict:
    return json.loads(blobs.data[artifacts.rows[0].sha256])


# --- opening ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_blank_canvas_states_the_coordinate_system(monkeypatch) -> None:
    # The first result IS the usage guide — delivered on a call the model was making
    # anyway, rather than costing a tool slot.
    handlers, _b, _a, _p = _build(monkeypatch)
    out = await handlers["canvas"]({"ops": [{"op": "text", "x": 5, "y": 20, "text": "hi"}]}, _ctx())
    assert "TOP-LEFT" in out and "1024x768" in out


@pytest.mark.asyncio
async def test_canvas_on_a_photo_adopts_its_pixel_frame(monkeypatch) -> None:
    handlers, blobs, artifacts, _p = _build(monkeypatch, source=_photo(640, 480))
    out = await handlers["canvas"]({"on_attachment_id": "att-1"}, _ctx())
    assert "640x480" in out
    scene = _scene_of(blobs, artifacts)
    assert scene["background"] == {
        "kind": "attachment",
        "width": 640,
        "height": 480,
        "ref": "att-1",
    }


@pytest.mark.asyncio
async def test_two_sources_is_refused(monkeypatch) -> None:
    handlers, _b, _a, _p = _build(monkeypatch, source=_photo())
    out = await handlers["canvas"]({"on_attachment_id": "a", "on_image_id": "b"}, _ctx())
    assert "not both" in out


@pytest.mark.asyncio
async def test_an_unresolvable_source_is_a_clean_error(monkeypatch) -> None:
    handlers, _b, artifacts, _p = _build(monkeypatch, source="that attachment isn't an image")
    out = await handlers["canvas"]({"on_attachment_id": "att-1"}, _ctx())
    assert out == "that attachment isn't an image"
    assert artifacts.rows == []  # nothing persisted for a failed open


# --- persistence ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_scene_upserts_one_row_per_canvas(monkeypatch) -> None:
    handlers, blobs, artifacts, _p = _build(monkeypatch)
    first = await handlers["canvas"]({"ops": [{"op": "text", "x": 1, "y": 9, "text": "a"}]}, _ctx())
    canvas_id = first.split()[1]
    await handlers["canvas"](
        {"canvas_id": canvas_id, "ops": [{"op": "text", "x": 2, "y": 9, "text": "b"}]}, _ctx()
    )
    assert len(artifacts.rows) == 1  # upsert, not append
    assert artifacts.rows[0].kind == ARTIFACT_KIND
    assert len(_scene_of(blobs, artifacts)["elements"]) == 2


@pytest.mark.asyncio
async def test_an_unknown_canvas_id_lists_what_exists(monkeypatch) -> None:
    handlers, _b, _a, _p = _build(monkeypatch)
    first = await handlers["canvas"]({"ops": [{"op": "text", "x": 1, "y": 9, "text": "a"}]}, _ctx())
    canvas_id = first.split()[1]
    out = await handlers["canvas"]({"canvas_id": "cvnope"}, _ctx())
    assert "No canvas 'cvnope'" in out and canvas_id in out


@pytest.mark.asyncio
async def test_no_chat_session_is_a_clean_refusal(monkeypatch) -> None:
    handlers, _b, _a, _p = _build(monkeypatch)
    out = await handlers["canvas"]({"ops": []}, _ctx(agent_session_id=None))
    assert "only exists inside a chat" in out


# --- the result the model reads --------------------------------------------


@pytest.mark.asyncio
async def test_partial_apply_is_reported_with_the_marks(monkeypatch) -> None:
    handlers, _b, _a, _p = _build(monkeypatch)
    out = await handlers["canvas"](
        {
            "ops": [
                {"op": "rect", "x": 10, "y": 10, "w": 50, "h": 50},
                {"op": "arrow", "x": 1, "y": 1},  # no endpoint
            ]
        },
        _ctx(),
    )
    assert "applied 1 of 2" in out
    assert "op 2 (arrow) skipped" in out
    assert "e1 rect(10,10 50x50)" in out


@pytest.mark.asyncio
async def test_every_result_nudges_toward_show_canvas(monkeypatch) -> None:
    # Without this the model draws a perfect figure and never shows the owner.
    handlers, _b, _a, _p = _build(monkeypatch)
    out = await handlers["canvas"]({"ops": []}, _ctx())
    assert "Nothing has been shown to the owner yet" in out
    assert "show_canvas" in out


@pytest.mark.asyncio
async def test_ops_must_be_a_list(monkeypatch) -> None:
    handlers, _b, _a, _p = _build(monkeypatch)
    assert "must be a list" in await handlers["canvas"]({"ops": {"op": "text"}}, _ctx())


# --- look -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_look_runs_a_separate_vision_call_and_returns_prose(monkeypatch) -> None:
    router = _Router("the red box is around the heater")
    handlers, _b, _a, _p = _build(monkeypatch, router=router)
    out = await handlers["canvas"](
        {"ops": [{"op": "rect", "x": 5, "y": 5, "w": 40, "h": 40}], "look": "is the box on it?"},
        _ctx(canvas_look_budget=ToolCallBudget(limit=LOOK_BUDGET)),
    )
    assert "the red box is around the heater" in out
    assert router.calls[0]["task"] == "agent.vision"
    # The image goes to the VISION model, never back into the agent's own context.
    assert len(router.calls[0]["images"]) == 1


@pytest.mark.asyncio
async def test_look_is_capped_and_says_how_many_are_left(monkeypatch) -> None:
    router = _Router()
    handlers, _b, _a, _p = _build(monkeypatch, router=router)
    budget = ToolCallBudget(limit=2)
    ctx = _ctx(canvas_look_budget=budget)
    first = await handlers["canvas"]({"ops": [], "look": "q"}, ctx)
    assert "(1 look(s) left)" in first
    canvas_id = first.split()[1]
    await handlers["canvas"]({"canvas_id": canvas_id, "look": "q"}, ctx)
    third = await handlers["canvas"]({"canvas_id": canvas_id, "look": "q"}, ctx)
    assert "used all your looks" in third
    assert len(router.calls) == 2  # the third never reached the vision model


@pytest.mark.asyncio
async def test_a_vision_failure_does_not_lose_the_drawing(monkeypatch) -> None:
    class _Boom(_Router):
        async def complete(self, task: str, **kw: Any):
            from jbrain.llm.errors import LlmError

            raise LlmError("vision model unreachable")

    handlers, blobs, artifacts, _p = _build(monkeypatch, router=_Boom())
    out = await handlers["canvas"](
        {"ops": [{"op": "rect", "x": 5, "y": 5, "w": 40, "h": 40}], "look": "q"},
        _ctx(canvas_look_budget=ToolCallBudget(limit=LOOK_BUDGET)),
    )
    assert "Couldn't look at the canvas" in out
    assert len(_scene_of(blobs, artifacts)["elements"]) == 1  # the mark still landed


# --- the call ceiling -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_call_budget_is_enforced_by_the_engine(monkeypatch) -> None:
    # A /chat turn allows 500 steps, so nothing else stops a fiddle loop.
    handlers, _b, _a, _p = _build(monkeypatch)
    budget = ToolCallBudget(limit=2)
    ctx = _ctx(canvas_call_budget=budget)
    await handlers["canvas"]({"ops": []}, ctx)
    await handlers["canvas"]({"ops": []}, ctx)
    out = await handlers["canvas"]({"ops": []}, ctx)
    assert "that is the cap" in out and "show_canvas" in out


def test_the_shipped_ceilings_match_the_ratified_decision() -> None:
    assert (CALL_BUDGET, LOOK_BUDGET) == (10, 3)


# --- show_canvas ------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_canvas_persists_with_the_canvas_provenance(monkeypatch) -> None:
    handlers, _b, _a, persisted = _build(monkeypatch)
    first = await handlers["canvas"](
        {"ops": [{"op": "rect", "x": 5, "y": 5, "w": 40, "h": 40}]}, _ctx()
    )
    canvas_id = first.split()[1]
    out = await handlers["show_canvas"]({"canvas_id": canvas_id, "caption": "the thing"}, _ctx())
    assert persisted[0]["provenance"] == PROVENANCE_CANVAS
    assert persisted[0]["data"][:8] == b"\x89PNG\r\n\x1a\n"
    # Stamped provenance keeps canvases out of the owner's image gallery, which filters
    # on provenance IS NULL.
    assert "image_id img1" in out
    assert "do not paste a link" in out


@pytest.mark.asyncio
async def test_show_canvas_refuses_an_empty_canvas(monkeypatch) -> None:
    handlers, _b, _a, persisted = _build(monkeypatch)
    first = await handlers["canvas"]({"ops": []}, _ctx())
    canvas_id = first.split()[1]
    out = await handlers["show_canvas"]({"canvas_id": canvas_id}, _ctx())
    assert "no marks on it yet" in out
    assert persisted == []


@pytest.mark.asyncio
async def test_show_canvas_needs_an_id(monkeypatch) -> None:
    handlers, _b, _a, _p = _build(monkeypatch)
    assert "needs the canvas_id" in await handlers["show_canvas"]({}, _ctx())


@pytest.mark.asyncio
async def test_show_canvas_says_the_canvas_stays_editable(monkeypatch) -> None:
    # The correction loop depends on the model knowing it can keep drawing after showing.
    handlers, _b, _a, _p = _build(monkeypatch)
    first = await handlers["canvas"](
        {"ops": [{"op": "rect", "x": 5, "y": 5, "w": 40, "h": 40}]}, _ctx()
    )
    canvas_id = first.split()[1]
    out = await handlers["show_canvas"]({"canvas_id": canvas_id}, _ctx())
    assert "stays editable" in out and canvas_id in out
