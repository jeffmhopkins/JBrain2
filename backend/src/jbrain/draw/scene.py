"""The canvas scene: a retained, id-addressed element list and the op fold over it.

Pure — no DB, no LLM, no network, no rendering (docs/plans/AGENT_CANVAS_PLAN.md §4).

**Why retained rather than a mutable bitmap.** Editing by id is what makes "move that
box left" a one-op edit instead of a redraw. That is not a preference: instruction-
based editing where the model regenerates geometry as text measurably drifts — on the
published SVG-edit benchmarks most models score WORSE than not editing at all. A
`{"op": "move", "id": "e4", "dx": -40}` never asks the model to re-derive a coordinate
it already got right.

**Why the model speaks pixels and this stores fractions.** Models reason better about
"x=610 on a 1024-wide canvas" than about 0.5957, and the sidecar states the canvas
size, so ops arrive in pixels. Fractions are what get *stored*, because they survive a
background swap and a re-render at another DPI, and because they are one division away
from the model's own grounding output. Text size stays in pixels: unlike a position, a
28px label does not want to follow a background swap.

**Why ops are one flat shape with no `enum`.** gpt-oss's harmony path (llama.cpp
`--jinja`) builds a GBNF grammar over the tool union, and an `enum` on a property of a
many-optional-property object deterministically segfaults the upstream — bisected and
documented at `agent/tools/analyze_stream.tool`. So the op vocabulary lives in the
field description and is validated here instead of in the schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

# Semantic tones, never colours. `DESIGN.md` binds components to `tone`/`kind` enums
# and explicitly forbids model-authored colour or hex; the renderer owns the mapping.
# "auto" means "high contrast against whatever is underneath", resolved at render.
TONES = ("auto", "danger", "warn", "info", "ok", "accent", "neutral")

# The op vocabulary. Deliberately high-level: `callout` and `label_box` are composites
# that would each be four primitives, and they are the two that do the actual work of
# annotating a photo. The model never computes a leader line or an arrowhead polygon.
DRAW_OPS = ("text", "line", "arrow", "rect", "callout", "label_box", "html")
EDIT_OPS = ("delete", "move", "clear")
ALL_OPS = (*DRAW_OPS, *EDIT_OPS)

# `html` places a browser-rendered block at a rect (AGENT_CANVAS_PLAN §3b). It exists
# because the shape ops are worst at exactly what HTML is best at — wrapped multi-line
# text, lists, small tables, styled labels — and because HTML is a language the model
# is genuinely fluent in, where this op vocabulary is one it meets for the first time
# in the sidecar. It stays an OP rather than a separate tool so the surface doesn't
# grow, and so a rendered block is still an element: movable and deletable by id like
# any other mark, because the OP owns the rect while the markup only fills it.
MAX_HTML_CHARS = 8000

# Defaults tuned for legibility at phone scale over a photograph, not for subtlety.
# These are the FLOOR, for a ~1024px sheet; `_scaled_default` grows them with the
# canvas, because a canvas is often a 4080px phone photo and an absolute 3px stroke on
# one of those renders as a hairline — observed live on the first real annotation, where
# the boxes were correct but nearly invisible. Proportions, not absolutes.
DEFAULT_TEXT_PX = 28.0
DEFAULT_STROKE_PX = 3.0
# Fractions of the canvas's LONG edge. 1.2% ≈ 49px of text and 0.25% ≈ 10px of stroke on
# a 4080px photo; both are no-ops at 1024px, so a blank sheet is byte-identical.
TEXT_FRACTION = 0.012
STROKE_FRACTION = 0.0025
MIN_TEXT_PX = 8.0
MAX_TEXT_PX = 200.0
MIN_STROKE_PX = 1.0
MAX_STROKE_PX = 40.0

MAX_ELEMENTS = 200  # a figure, not a raster; past this the model is thrashing
MAX_TEXT_CHARS = 240  # a label, not a paragraph


class SceneError(ValueError):
    """The scene itself is unusable (bad persisted JSON, impossible canvas)."""


@dataclass(frozen=True, slots=True)
class Background:
    """What the elements are drawn on top of.

    `ref` is the attachment/image id for a photo-backed canvas and None for a blank
    sheet. `width`/`height` are the EXIF-corrected pixel dimensions of that source —
    the coordinate frame everything else is a fraction of."""

    kind: str  # "blank" | "attachment" | "image"
    width: int
    height: int
    ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "width": self.width, "height": self.height, "ref": self.ref}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Background:
        try:
            return cls(
                kind=str(raw["kind"]),
                width=int(raw["width"]),
                height=int(raw["height"]),
                ref=raw.get("ref"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SceneError(f"unreadable canvas background: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Element:
    """One mark. `at` holds fractional (0..1) coordinates whose keys depend on `kind`.

    `origin` records where the coordinate came from — "vlm" when the model guessed it,
    "ocr"/"detector" when a deterministic detector supplied it, "owner" when it was
    stated. When a box lands wrong, that is the first thing anyone wants to know."""

    id: str
    kind: str
    at: dict[str, float]
    text: str = ""
    tone: str = "auto"
    size: float = DEFAULT_TEXT_PX
    stroke: float = DEFAULT_STROKE_PX
    fill: bool = False
    origin: str = "vlm"
    # Model-authored markup, for `html` elements only. Persisted verbatim: it is the
    # source the block re-renders from, so a canvas reopened next turn is still
    # editable rather than frozen as pixels.
    html: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "at": dict(self.at),
            "text": self.text,
            "tone": self.tone,
            "size": self.size,
            "stroke": self.stroke,
            "fill": self.fill,
            "origin": self.origin,
            "html": self.html,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Element:
        try:
            return cls(
                id=str(raw["id"]),
                kind=str(raw["kind"]),
                at={str(k): float(v) for k, v in dict(raw["at"]).items()},
                text=str(raw.get("text", "")),
                tone=str(raw.get("tone", "auto")),
                size=float(raw.get("size", DEFAULT_TEXT_PX)),
                stroke=float(raw.get("stroke", DEFAULT_STROKE_PX)),
                fill=bool(raw.get("fill", False)),
                origin=str(raw.get("origin", "vlm")),
                html=str(raw.get("html", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SceneError(f"unreadable canvas element: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Scene:
    """A canvas: background + ordered elements. Rendered whole, every time."""

    canvas_id: str
    background: Background
    elements: tuple[Element, ...] = ()
    next_seq: int = 1
    v: int = 1

    @property
    def width(self) -> int:
        return self.background.width

    @property
    def height(self) -> int:
        return self.background.height

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "canvas_id": self.canvas_id,
            "background": self.background.to_dict(),
            "elements": [e.to_dict() for e in self.elements],
            "next_seq": self.next_seq,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Scene:
        try:
            return cls(
                v=int(raw.get("v", 1)),
                canvas_id=str(raw["canvas_id"]),
                background=Background.from_dict(dict(raw["background"])),
                elements=tuple(Element.from_dict(dict(e)) for e in raw.get("elements", [])),
                next_seq=int(raw.get("next_seq", 1)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SceneError(f"unreadable canvas scene: {exc}") from exc

    def summary(self) -> str:
        """One compact line per mark, for the tool result.

        The full list rides every response because it is short and because it is what
        makes editing-by-id usable without spending a round trip on a `list` op."""
        if not self.elements:
            return "no marks yet"
        return " ".join(_describe(e, self.width, self.height) for e in self.elements)


@dataclass(slots=True)
class OpReport:
    """What the fold did. Partial application is deliberate: a 20-op figure with one
    typo must not lose the other 19 on a model running ~7 t/s, and because the scene
    is addressable the model just re-sends the one op."""

    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def line(self, total: int) -> str:
        parts = [f"applied {len(self.applied)} of {total}"]
        if self.applied:
            parts[0] += " → " + ", ".join(self.applied)
        return "\n".join([*parts, *self.skipped, *self.notes])


def apply_ops(scene: Scene, ops: list[dict[str, Any]]) -> tuple[Scene, OpReport]:
    """Fold a batch of flat ops onto `scene`, returning the new scene and a report.

    Never raises for a bad op — a malformed one is skipped with an actionable reason
    and the rest still apply. Out-of-range coordinates are CLAMPED rather than
    rejected: a box a few percent off the edge is a good box with a bad edge, and
    trimming it is what the owner wants."""
    report = OpReport()
    elements = list(scene.elements)
    next_seq = scene.next_seq
    for index, raw in enumerate(ops, start=1):
        if not isinstance(raw, dict):
            report.skipped.append(
                f"op {index} skipped: expected an object, got {type(raw).__name__}"
            )
            continue
        name = str(raw.get("op", "")).strip().lower()
        if name not in ALL_OPS:
            report.skipped.append(
                f"op {index} skipped: unknown op {name!r} — use one of {', '.join(ALL_OPS)}"
            )
            continue
        try:
            if name == "clear":
                elements = []
                report.applied.append("clear")
            elif name == "delete":
                elements, note = _delete(elements, raw)
                report.applied.append(note)
            elif name == "move":
                elements, note = _move(elements, raw, scene.width, scene.height)
                report.applied.append(note)
            else:
                if len(elements) >= MAX_ELEMENTS:
                    report.skipped.append(
                        f"op {index} skipped: canvas already holds {MAX_ELEMENTS} marks — "
                        f"delete some or clear before adding more"
                    )
                    continue
                element, notes = _build(f"e{next_seq}", name, raw, scene.width, scene.height)
                elements.append(element)
                next_seq += 1
                report.applied.append(f"{element.id} {name}")
                report.notes.extend(f"op {index}: {n}" for n in notes)
        except _OpRejected as exc:
            report.skipped.append(f"op {index} ({name}) skipped: {exc}")
    return replace(scene, elements=tuple(elements), next_seq=next_seq), report


class _OpRejected(ValueError):
    """This one op can't be applied; the rest of the batch still can."""


def _delete(elements: list[Element], raw: dict[str, Any]) -> tuple[list[Element], str]:
    target = str(raw.get("id", "")).strip()
    if not target:
        raise _OpRejected('delete needs the id of an existing mark (e.g. "e3")')
    kept = [e for e in elements if e.id != target]
    if len(kept) == len(elements):
        known = ", ".join(e.id for e in elements) or "none"
        raise _OpRejected(f"no mark {target!r} on this canvas — marks are: {known}")
    return kept, f"deleted {target}"


def _move(
    elements: list[Element], raw: dict[str, Any], width: int, height: int
) -> tuple[list[Element], str]:
    target = str(raw.get("id", "")).strip()
    if not target:
        raise _OpRejected('move needs the id of an existing mark (e.g. "e3")')
    dx = _number(raw, "dx", 0.0) / width
    dy = _number(raw, "dy", 0.0) / height
    if dx == 0.0 and dy == 0.0:
        raise _OpRejected("move needs a non-zero dx and/or dy, in pixels")
    out: list[Element] = []
    found = False
    for element in elements:
        if element.id != target:
            out.append(element)
            continue
        found = True
        shifted = {
            key: _clamp01(value + (dx if key.startswith("x") else dy))
            for key, value in element.at.items()
        }
        out.append(replace(element, at=shifted))
    if not found:
        known = ", ".join(e.id for e in elements) or "none"
        raise _OpRejected(f"no mark {target!r} on this canvas — marks are: {known}")
    return out, f"moved {target}"


def _build(
    element_id: str, kind: str, raw: dict[str, Any], width: int, height: int
) -> tuple[Element, list[str]]:
    notes: list[str] = []
    text = _text(raw)
    html = ""
    tone = str(raw.get("tone", "auto")).strip().lower() or "auto"
    if tone not in TONES:
        notes.append(f"unknown tone {tone!r} — using auto (tones: {', '.join(TONES)})")
        tone = "auto"
    long_edge = max(width, height)
    size = _bounded(
        _number(raw, "size", _scaled_default(DEFAULT_TEXT_PX, TEXT_FRACTION, long_edge)),
        MIN_TEXT_PX,
        MAX_TEXT_PX,
    )
    stroke = _bounded(
        _number(raw, "width", _scaled_default(DEFAULT_STROKE_PX, STROKE_FRACTION, long_edge)),
        MIN_STROKE_PX,
        MAX_STROKE_PX,
    )

    if kind == "text":
        if not text:
            raise _OpRejected("text needs `text` to draw")
        at = _point(raw, "x", "y", width, height, notes)
    elif kind in ("line", "arrow"):
        at = {**_point(raw, "x", "y", width, height, notes)}
        at.update(_point(raw, "x2", "y2", width, height, notes, suffix="2"))
        if at["x"] == at["x2"] and at["y"] == at["y2"]:
            raise _OpRejected(f"{kind} start and end are the same point — nothing to draw")
    elif kind == "rect":
        at = _box(raw, width, height, notes)
    elif kind == "label_box":
        if not text:
            raise _OpRejected("label_box needs `text` for its caption")
        at = _box(raw, width, height, notes)
    elif kind == "callout":
        if not text:
            raise _OpRejected("callout needs `text` — it is a labelled pointer")
        at = {**_point(raw, "x", "y", width, height, notes)}
        at.update(_point(raw, "x2", "y2", width, height, notes, suffix="2"))
    elif kind == "html":
        markup = str(raw.get("html", "") or "").strip()
        if not markup:
            raise _OpRejected("html needs `html` — the markup to render in the box")
        if len(markup) > MAX_HTML_CHARS:
            raise _OpRejected(
                f"that html is {len(markup)} chars — keep a block under {MAX_HTML_CHARS}"
            )
        html = markup
        at = _box(raw, width, height, notes)
    else:  # pragma: no cover - ALL_OPS is the gate above
        raise _OpRejected(f"unhandled op {kind!r}")
    return (
        Element(
            id=element_id,
            kind=kind,
            at=at,
            text=text,
            tone=tone,
            size=size,
            stroke=stroke,
            fill=bool(raw.get("fill", False)),
            origin=str(raw.get("origin", "vlm")),
            html=html,
        ),
        notes,
    )


def _text(raw: dict[str, Any]) -> str:
    value = raw.get("text", "")
    text = "" if value is None else str(value).strip()
    # Truncate rather than reject: a model that rambled into the label still meant the
    # first clause, and losing the whole mark over it helps nobody.
    return text[:MAX_TEXT_CHARS]


def _number(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _OpRejected(f"{key} must be a number, got {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise _OpRejected(f"{key} must be a finite number, got {value!r}")
    return number


def _require(raw: dict[str, Any], key: str) -> float:
    if raw.get(key) is None:
        raise _OpRejected(f"missing {key} (pixels from the top-left; y grows downward)")
    return _number(raw, key, 0.0)


def _point(
    raw: dict[str, Any],
    x_key: str,
    y_key: str,
    width: int,
    height: int,
    notes: list[str],
    suffix: str = "",
) -> dict[str, float]:
    x_px, y_px = _require(raw, x_key), _require(raw, y_key)
    x = _clamp_axis(x_px, width, x_key, notes)
    y = _clamp_axis(y_px, height, y_key, notes)
    return {f"x{suffix}": x, f"y{suffix}": y}


def _box(raw: dict[str, Any], width: int, height: int, notes: list[str]) -> dict[str, float]:
    x_px, y_px = _require(raw, "x"), _require(raw, "y")
    w_px, h_px = _require(raw, "w"), _require(raw, "h")
    if w_px <= 0 or h_px <= 0:
        raise _OpRejected(f"w and h must be positive, got w={w_px:g} h={h_px:g}")
    # Stored as corners, so a later `move` shifts both edges with one rule.
    x1 = _clamp_axis(x_px, width, "x", notes)
    y1 = _clamp_axis(y_px, height, "y", notes)
    x2 = _clamp_axis(x_px + w_px, width, "x+w", notes)
    y2 = _clamp_axis(y_px + h_px, height, "y+h", notes)
    if x2 <= x1 or y2 <= y1:
        raise _OpRejected("that box is entirely off-canvas")
    return {"x": x1, "y": y1, "x2": x2, "y2": y2}


def _clamp_axis(value_px: float, extent: int, label: str, notes: list[str]) -> float:
    """Pixels → fraction, clamped in-bounds, REPORTING the clamp.

    Silently drawing off-screen is the failure the model can't see and won't fix; a
    note in the result is what lets it correct the next op."""
    fraction = value_px / extent
    if fraction < 0.0 or fraction > 1.0:
        notes.append(f"{label}={value_px:g} is outside 0..{extent} — clamped to the canvas edge")
    return _clamp01(fraction)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _scaled_default(floor: float, fraction: float, long_edge: int) -> float:
    """A default that grows with the canvas but never shrinks below the small-sheet floor.

    An explicit `size`/`width` from the model always wins — this only decides what an
    OMITTED one means, which is exactly where an absolute default silently fails: the
    model asked for a box, got a correct box, and could barely see it."""
    return max(floor, round(long_edge * fraction))


def _bounded(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _describe(element: Element, width: int, height: int) -> str:
    """A mark in the model's own units (pixels), so the summary feeds straight back
    into the next op without the model doing arithmetic."""
    at = element.at

    def px(key: str, extent: int) -> int:
        return round(at.get(key, 0.0) * extent)

    if element.kind in ("rect", "label_box", "html"):
        left, top = px("x", width), px("y", height)
        body = f"({left},{top} {px('x2', width) - left}x{px('y2', height) - top})"
    elif element.kind in ("line", "arrow", "callout"):
        body = f"({px('x', width)},{px('y', height)}→{px('x2', width)},{px('y2', height)})"
    else:
        body = f"({px('x', width)},{px('y', height)})"
    if element.kind == "html":
        # The markup itself would swamp the summary, and the model already has it in
        # its own context — a length is enough to identify the block.
        label = f" [{len(element.html)} chars of html]"
    else:
        label = f' "{element.text}"' if element.text else ""
    return f"{element.id} {element.kind}{body}{label}"
