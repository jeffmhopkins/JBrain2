"""Model grounding output → image coordinates (docs/plans/AGENT_CANVAS_PLAN.md §5).

Pure and dependency-free, like `chartscale.py`, so the canvas tools, the crop tool
and the debug probe all convert the same way and none of them imports another.

**Why this module exists at all.** The coordinate convention is a per-MODEL fact and
it has already changed once upstream: Qwen2.5-VL emitted absolute pixels *in the
smart-resized input frame*, Qwen3-VL emits coordinates normalized against the
ORIGINAL image. Worse, the Qwen3-VL documentation contradicts itself on the
normalization base — the 2d_grounding cookbook divides by 1000, the docs site
describes a 0–1 range — and the `Qwen/Qwen3.8-27B` model card documents no grounding
format at all. Scattering `/1000 * width` at three call sites is how those
conventions get silently mixed, and the failure is not loud: a wrong base produces a
confident box around the wrong thing. So every conversion goes through here, keyed on
the SERVED model, and an unrecognised model raises rather than guessing.

Normalization is per-axis and independent. Scaling both axes by the long side looks
nearly right on a square-ish image and drifts badly on a portrait phone photo, which
is exactly the reported failure mode for far-from-square aspect ratios upstream.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

# Model families whose grounding we have qualified. Keyed by the SERVED model name
# (`LocalModel.served_model`), not the catalog id, because that is what the router
# resolves and what the gateway actually loads.
#
# AUTO is not a cop-out: the two candidate bases are trivially separable in practice
# (see `infer_convention`), and it is the honest state until the W0 probe measures
# this checkpoint on the owner's box. Pin the measured value here afterwards — the
# probe reports which base it inferred precisely so this table can be made explicit.


class Convention(StrEnum):
    """How a model's grounding numbers map onto the original image."""

    NORM_1000 = "norm_1000"  # coord / 1000 * dimension — the Qwen3-VL cookbook base
    NORM_1 = "norm_1"  # coord * dimension — the Qwen3-VL docs-site base
    AUTO = "auto"  # infer per response; only for models not yet measured


_CONVENTIONS: dict[str, Convention] = {
    # The two Qwen3.8 vision twins: same weights, same repo, same projector, so one
    # probe qualifies both. AUTO until W0 runs on-box (AGENT_CANVAS_PLAN §10.3).
    "qwen3.8-27b": Convention.AUTO,
    "qwen3.8-27b-q4": Convention.AUTO,
}

# `bbox_2d` is [x1, y1, x2, y2] — CORNERS, not [x, y, w, h]. A silent w/h misread
# yields boxes that look roughly right and drift, which is the hardest variant to
# spot, so the corner reading is asserted here rather than assumed at call sites.
_BOX_KEYS = ("bbox_2d", "bbox", "box_2d", "box")
_POINT_KEYS = ("point_2d", "point")
_LABEL_KEYS = ("label", "text", "name")

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class GroundingError(ValueError):
    """The model's grounding output could not be trusted."""


class UnknownGroundingModel(GroundingError):
    """No qualified convention for this served model — refuse rather than guess."""


@dataclass(frozen=True, slots=True)
class RawBox:
    """A box exactly as the model emitted it, in whatever base it used."""

    x1: float
    y1: float
    x2: float
    y2: float
    label: str = ""


@dataclass(frozen=True, slots=True)
class RawPoint:
    x: float
    y: float
    label: str = ""


@dataclass(frozen=True, slots=True)
class FractionBox:
    """A box in 0..1 of the original image — the canvas scene's storage unit."""

    x1: float
    y1: float
    x2: float
    y2: float
    label: str = ""


@dataclass(frozen=True, slots=True)
class PixelBox:
    """A box in original-image pixels, clamped in-bounds — the crop unit."""

    x1: int
    y1: int
    x2: int
    y2: int
    label: str = ""

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1


def convention_for(served_model: str) -> Convention:
    """The qualified convention for a served model, or raise.

    Refusing beats guessing: an unqualified model that happens to emit plausible
    numbers would produce plausible, wrong boxes, and nothing downstream can tell."""
    try:
        return _CONVENTIONS[served_model]
    except KeyError:
        raise UnknownGroundingModel(
            f"no qualified grounding convention for served model {served_model!r} — "
            f"qualified: {sorted(_CONVENTIONS)}. Run POST /api/debug/grounding against "
            f"it and pin the measured base in agent/grounding.py before using it."
        ) from None


def infer_convention(values: list[float]) -> Convention:
    """Which base these numbers are in, inferred from their magnitude.

    Safe because the two bases are separable in practice, not in theory: a box in
    0–1000 space whose every coordinate is ≤ 1 would occupy the top-left 0.1% of the
    image. The ambiguity is real only for a degenerate box at the origin, which is
    not a box anyone asked for. Used by AUTO models and reported by the probe."""
    if not values:
        raise GroundingError("no coordinates to infer a convention from")
    return Convention.NORM_1 if max(values) <= 1.0 else Convention.NORM_1000


def _divisor(convention: Convention, values: list[float]) -> float:
    if convention is Convention.AUTO:
        convention = infer_convention(values)
    return 1000.0 if convention is Convention.NORM_1000 else 1.0


def _coerce(entry: dict, keys: tuple[str, ...], arity: int) -> list[float] | None:
    for key in keys:
        raw = entry.get(key)
        if isinstance(raw, list | tuple) and len(raw) == arity:
            try:
                return [float(v) for v in raw]
            except (TypeError, ValueError):
                return None
    return None


def _label(entry: dict) -> str:
    for key in _LABEL_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_grounding(text: str) -> tuple[list[RawBox], list[RawPoint]]:
    """Pull boxes and points out of a model's grounding reply.

    Tolerant by necessity: the reply may be bare JSON, fenced, or JSON with prose
    wrapped around it, and a thinking model may emit all three shapes across a
    session. Returns empty lists rather than raising when there is nothing to find —
    "found nothing" is a real, reportable answer (see the undercount note in
    AGENT_CANVAS_PLAN §6.5), not an error."""
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        entries = parsed if isinstance(parsed, list) else [parsed]
        boxes: list[RawBox] = []
        points: list[RawPoint] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if (nums := _coerce(entry, _BOX_KEYS, 4)) is not None:
                boxes.append(RawBox(*nums, label=_label(entry)))
            elif (nums := _coerce(entry, _POINT_KEYS, 2)) is not None:
                points.append(RawPoint(*nums, label=_label(entry)))
        if boxes or points:
            return boxes, points
    return [], []


def _json_candidates(text: str) -> list[str]:
    """Fenced blocks first, then the outermost bracketed span, then the whole string.

    Fenced first because a thinking model often reasons in prose containing stray
    brackets before emitting the real answer in a fence."""
    candidates = [m.group(1).strip() for m in _JSON_BLOCK.finditer(text)]
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.append(text.strip())
    return candidates


def to_fractions(
    boxes: list[RawBox], *, served_model: str, convention: Convention | None = None
) -> list[FractionBox]:
    """Model coordinates → 0..1 fractions of the original image.

    Fractions are the scene's storage unit (§4.2): they survive a background swap and
    a re-render at another DPI, and they are one division away from the model's own
    output. Corners are ordered and clamped, so a model that emits x2 < x1 (it
    happens) yields a valid box rather than a negative-width one."""
    if not boxes:
        return []
    resolved = convention or convention_for(served_model)
    flat = [v for b in boxes for v in (b.x1, b.y1, b.x2, b.y2)]
    div = _divisor(resolved, flat)
    out: list[FractionBox] = []
    for box in boxes:
        xs = sorted((_clamp01(box.x1 / div), _clamp01(box.x2 / div)))
        ys = sorted((_clamp01(box.y1 / div), _clamp01(box.y2 / div)))
        out.append(FractionBox(xs[0], ys[0], xs[1], ys[1], label=box.label))
    return out


def to_pixels(
    boxes: list[RawBox],
    *,
    served_model: str,
    width: int,
    height: int,
    convention: Convention | None = None,
) -> list[PixelBox]:
    """Model coordinates → original-image pixels.

    `width`/`height` MUST be the EXIF-corrected dimensions of the image the model was
    actually handed. Taking them from an untransposed header while the model saw a
    rotated frame transposes every box 90° — the single highest-probability bug in
    this feature (§5.2), which is why `chat_images.image_dimensions` now transposes."""
    if width <= 0 or height <= 0:
        raise GroundingError(f"image dimensions must be positive, got {width}x{height}")
    fractions = to_fractions(boxes, served_model=served_model, convention=convention)
    out: list[PixelBox] = []
    for frac in fractions:
        # Per-axis and independent — never a single uniform scale (§5.3).
        x1, x2 = round(frac.x1 * width), round(frac.x2 * width)
        y1, y2 = round(frac.y1 * height), round(frac.y2 * height)
        out.append(PixelBox(x1, y1, x2, y2, label=frac.label))
    return out


def _clamp01(value: float) -> float:
    """Clamp, never reject: a box a few percent off the edge is a good box with a bad
    edge, and trimming it is what the owner wants (the annotation prior art converged
    on auto-adjusting into bounds rather than erroring)."""
    if value != value:  # NaN — a model emitting one is a parse failure, not a box
        raise GroundingError("coordinate was NaN")
    return min(1.0, max(0.0, value))
