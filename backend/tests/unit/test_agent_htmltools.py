"""The `render_html` tool handler (jbrain.agent.htmltools) — AGENT_CANVAS_PLAN §3b.

Collaborators are faked (no DB, no LLM, no sidecar) so these test the handler's own
contract: what reaches the renderer, what the model is told, and the two properties the
tool exists to guarantee — that the image is legible to a vision model, and that it
carries no frame of its own for the app's card to double.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from jbrain.agent.htmltools import (
    DEFAULT_PAD,
    DEFAULT_WIDTH,
    MAX_WIDTH,
    MIN_WIDTH,
    PROVENANCE_HTML,
    RENDER_SCALE,
    build_html_handlers,
)
from jbrain.agent.loop import HTML_RENDER_BUDGET, ToolCallBudget, ToolContext
from jbrain.db.session import SessionContext
from jbrain.htmlrender import HtmlRenderError, Rendered

PNG = b"\x89PNG\r\n\x1a\nfake"


# --- fakes ------------------------------------------------------------------


@dataclass
class _Row:
    id: str = "img1"
    provenance: str = PROVENANCE_HTML
    width: int = 900
    height: int = 412


class _Html:
    """The sidecar client: records the call, replays a scripted outcome."""

    def __init__(self, *, configured: bool = True, error: str | None = None, **rendered: Any):
        self.configured = configured
        self.error = error
        self.calls: list[dict] = []
        self.rendered = Rendered(png=PNG, width=900, height=412, **rendered)

    async def render(self, html: str, **kw: Any) -> Rendered:
        self.calls.append({"html": html, **kw})
        if self.error:
            raise HtmlRenderError(self.error)
        return self.rendered


class _Router:
    def __init__(self, text: str = "It reads cleanly; nothing is cut off.") -> None:
        self.text = text
        self.calls: list[dict] = []

    async def supports_vision(self, task: str, spec_override: str | None = None) -> bool:
        return True

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
    html_render_budget: ToolCallBudget | None = None


def _build(monkeypatch: pytest.MonkeyPatch, *, html: _Html | None = None, router=None):
    renderer = html or _Html()
    persisted: list[dict] = []

    async def _fake_persist(maker, ctx, blobs, repo, *, data, provenance, model, prompt):
        persisted.append({"data": data, "provenance": provenance, "model": model, "prompt": prompt})
        return _Row()

    monkeypatch.setattr("jbrain.agent.htmltools.persist_chat_image", _fake_persist)
    monkeypatch.setattr("jbrain.agent.htmltools.chat_image_view", lambda row: None)

    # `cast` at the seam keeps the fakes small (the house pattern, see test_drawtools).
    handlers = build_html_handlers(*cast(Any, (None, None, None, router or _Router(), renderer)))
    return handlers["render_html"], renderer, persisted


def _ctx(**kw) -> ToolContext:
    return _Ctx(**kw)  # type: ignore[return-value]


# --- the happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_renders_and_shows_in_one_call(monkeypatch) -> None:
    render, renderer, persisted = _build(monkeypatch)
    out = await render({"html": "<h1>Hi</h1>", "caption": "A greeting"}, _ctx())
    assert renderer.calls[0]["html"] == "<h1>Hi</h1>"
    # A first-class chat image, so analyze_image can resolve it by id in a later turn.
    assert persisted[0]["provenance"] == PROVENANCE_HTML
    assert persisted[0]["prompt"] == "A greeting"
    assert "img1" in out and "analyze_image" in out


@pytest.mark.asyncio
async def test_renders_opaque_at_2x_for_a_vision_model(monkeypatch) -> None:
    # The two properties that decide whether a vision model can read this at all: a
    # transparent PNG gets flattened onto an unknown colour downstream (light text then
    # vanishes), and every vision path downscales, so 1x type arrives soft.
    render, renderer, _p = _build(monkeypatch)
    await render({"html": "<p>x</p>"}, _ctx())
    assert renderer.calls[0]["transparent"] is False
    assert renderer.calls[0]["scale"] == RENDER_SCALE


@pytest.mark.asyncio
async def test_height_is_measured_and_the_page_owns_its_gutter(monkeypatch) -> None:
    # Both halves of "no second frame": no guessed height leaving a band of empty
    # ground, and a gutter the page supplies so the markup needs no padded outer div.
    render, renderer, _p = _build(monkeypatch)
    await render({"html": "<p>x</p>"}, _ctx())
    assert renderer.calls[0]["height"] is None
    assert renderer.calls[0]["pad"] == DEFAULT_PAD
    # The ground is the app's own surface token, so the image sits flush in the card.
    assert renderer.calls[0]["theme"] == "dark"


@pytest.mark.asyncio
@pytest.mark.parametrize(("given", "expected"), [(400, 400), ("400", 400), (99999, 2048)])
async def test_an_explicit_height_is_honoured_and_clamped(monkeypatch, given, expected) -> None:
    render, renderer, _p = _build(monkeypatch)
    await render({"html": "<p>x</p>", "height": given}, _ctx())
    assert renderer.calls[0]["height"] == expected


@pytest.mark.asyncio
async def test_a_junk_height_degrades_to_measured_not_to_a_number(monkeypatch) -> None:
    # Unlike width, height has a right answer when it is missing, so junk must land on
    # it rather than on some invented default that would band the card with empty ground.
    render, renderer, _p = _build(monkeypatch)
    await render({"html": "<p>x</p>", "height": "tall"}, _ctx())
    assert renderer.calls[0]["height"] is None


# --- argument handling ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ({}, DEFAULT_WIDTH),
        ({"width": 9000}, MAX_WIDTH),
        ({"width": 4}, MIN_WIDTH),
        ({"width": "wide"}, DEFAULT_WIDTH),
    ],
)
async def test_width_is_clamped_not_refused(monkeypatch, given: dict, expected: int) -> None:
    # The markup is the point and the width is a detail: a junk value takes the default
    # instead of costing the model a call to find out.
    render, renderer, _p = _build(monkeypatch)
    await render({"html": "<p>x</p>", **given}, _ctx())
    assert renderer.calls[0]["width"] == expected


@pytest.mark.asyncio
async def test_missing_html_is_a_clean_error(monkeypatch) -> None:
    render, renderer, _p = _build(monkeypatch)
    assert "needs `html`" in await render({"caption": "nothing"}, _ctx())
    assert renderer.calls == []


@pytest.mark.asyncio
async def test_unknown_theme_names_the_valid_ones(monkeypatch) -> None:
    render, renderer, _p = _build(monkeypatch)
    out = await render({"html": "<p>x</p>", "theme": "neon"}, _ctx())
    assert "light" in out and "dark" in out
    assert renderer.calls == []


# --- degradation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_sidecar_degrades_to_prose(monkeypatch) -> None:
    render, renderer, persisted = _build(monkeypatch, html=_Html(configured=False))
    out = await render({"html": "<p>x</p>"}, _ctx())
    assert "not configured" in out and "prose" in out
    assert renderer.calls == [] and persisted == []


@pytest.mark.asyncio
async def test_a_render_failure_is_recoverable(monkeypatch) -> None:
    render, _r, persisted = _build(monkeypatch, html=_Html(error="render failed: Timeout"))
    out = await render({"html": "<p>x</p>"}, _ctx())
    assert "Timeout" in out and persisted == []


@pytest.mark.asyncio
async def test_clipped_content_is_reported_not_swallowed(monkeypatch) -> None:
    # Half a table shown as if it were the whole one is the failure that gets found from
    # a wrong answer, so the model is told and given the fix.
    render, _r, _p = _build(monkeypatch, html=_Html(clipped=True))
    out = await render({"html": "<p>x</p>"}, _ctx())
    assert "cut off" in out and "split it" in out


# --- the budget -------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_render_spends_one_unit(monkeypatch) -> None:
    render, _r, _p = _build(monkeypatch)
    budget = ToolCallBudget(limit=HTML_RENDER_BUDGET)
    await render({"html": "<p>x</p>"}, _ctx(html_render_budget=budget))
    assert budget.used == 1


@pytest.mark.asyncio
async def test_an_exhausted_budget_refuses_before_the_browser(monkeypatch) -> None:
    # An engine cap, not a prompt one: a supervised turn has 500 steps, and each unit
    # here is a Chromium page on a memory-bound box.
    render, renderer, _p = _build(monkeypatch)
    budget = ToolCallBudget(limit=2, used=2)
    out = await render({"html": "<p>x</p>"}, _ctx(html_render_budget=budget))
    assert "that is the cap" in out
    assert renderer.calls == []


# --- the look ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_look_reads_the_pixels_it_just_made(monkeypatch) -> None:
    monkeypatch.setattr("jbrain.agent.htmltools.downscale_for_vision", lambda data, mt: (data, mt))
    router = _Router("The table is complete.")
    render, _r, _p = _build(monkeypatch, router=router)
    out = await render({"html": "<p>x</p>", "look": "is anything cut off?"}, _ctx())
    assert router.calls[0]["task"] == "agent.vision"
    assert router.calls[0]["images"][0].media_type == "image/png"
    assert "The table is complete." in out


@pytest.mark.asyncio
async def test_the_look_image_goes_through_the_vision_downscale(monkeypatch) -> None:
    # The render is 2x precisely so it survives this resize; skipping it would spend a
    # large chunk of context on an image the model cannot use any better.
    seen: list[tuple[bytes, str]] = []

    def _fake_downscale(data: bytes, media_type: str) -> tuple[bytes, str]:
        seen.append((data, media_type))
        return b"smaller", "image/jpeg"

    monkeypatch.setattr("jbrain.agent.htmltools.downscale_for_vision", _fake_downscale)
    router = _Router()
    render, _r, _p = _build(monkeypatch, router=router)
    await render({"html": "<p>x</p>", "look": "readable?"}, _ctx())
    assert seen == [(PNG, "image/png")]
    assert router.calls[0]["images"][0].media_type == "image/jpeg"


@pytest.mark.asyncio
async def test_no_look_means_no_vision_call(monkeypatch) -> None:
    router = _Router()
    render, _r, _p = _build(monkeypatch, router=router)
    await render({"html": "<p>x</p>"}, _ctx())
    assert router.calls == []


@pytest.mark.asyncio
async def test_a_failed_look_does_not_lose_the_render(monkeypatch) -> None:
    from jbrain.llm.errors import LlmError

    class _Broken(_Router):
        async def complete(self, task: str, **kw: Any):
            raise LlmError("vision model is cold")

    monkeypatch.setattr("jbrain.agent.htmltools.downscale_for_vision", lambda data, mt: (data, mt))
    render, _r, persisted = _build(monkeypatch, router=_Broken())
    out = await render({"html": "<p>x</p>", "look": "ok?"}, _ctx())
    # The image was already shown to the owner; a failed read must not retract it.
    assert persisted and "img1" in out and "Couldn't look at it" in out
