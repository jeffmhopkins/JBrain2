"""Coordinate conversion for model grounding output (AGENT_CANVAS_PLAN §5).

The failures these guard against are all SILENT — a wrong base, a transposed axis or
a corner/extent misread produces a plausible box in the wrong place, and nothing
downstream can tell. So the assertions are about exact pixels, not shapes.
"""

from __future__ import annotations

import pytest

from jbrain.agent.grounding import (
    Convention,
    FractionBox,
    GroundingError,
    RawBox,
    UnknownGroundingModel,
    convention_for,
    infer_convention,
    parse_grounding,
    to_fractions,
    to_pixels,
)

QWEN = "qwen3.8-27b"


# --- the convention table ---------------------------------------------------


def test_qualified_models_resolve() -> None:
    assert convention_for("qwen3.8-27b") is Convention.AUTO
    assert convention_for("qwen3.8-27b-q4") is Convention.AUTO


def test_unknown_model_refuses_rather_than_guessing() -> None:
    # The whole point of the module: an unqualified model must not silently get a
    # default base, because its boxes would look fine and be wrong.
    with pytest.raises(UnknownGroundingModel, match="no qualified grounding convention"):
        convention_for("some-new-vl-model")


def test_refusal_names_the_probe_route() -> None:
    with pytest.raises(UnknownGroundingModel, match="/api/debug/grounding"):
        convention_for("qwen3.8-27b-mtp")


# --- base inference ---------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([0.31, 0.5, 0.44, 0.62], Convention.NORM_1),
        ([310.0, 500.0, 440.0, 620.0], Convention.NORM_1000),
        ([1.0, 1.0, 1.0, 1.0], Convention.NORM_1),  # the boundary belongs to 0..1
        ([0.0, 0.0, 1000.0, 1000.0], Convention.NORM_1000),
    ],
)
def test_infer_convention(values: list[float], expected: Convention) -> None:
    assert infer_convention(values) is expected


def test_infer_convention_needs_values() -> None:
    with pytest.raises(GroundingError):
        infer_convention([])


# --- fractions --------------------------------------------------------------


def test_norm_1000_to_fractions() -> None:
    boxes = [RawBox(310, 500, 440, 620, label="heater")]
    assert to_fractions(boxes, served_model=QWEN) == [
        FractionBox(0.31, 0.5, 0.44, 0.62, label="heater")
    ]


def test_norm_1_passes_through() -> None:
    boxes = [RawBox(0.31, 0.5, 0.44, 0.62)]
    assert to_fractions(boxes, served_model=QWEN) == [FractionBox(0.31, 0.5, 0.44, 0.62)]


def test_inverted_corners_are_ordered_not_rejected() -> None:
    # Models do emit x2 < x1. A negative-width box would crash the renderer far from
    # here, so it is normalised at the boundary instead.
    (box,) = to_fractions([RawBox(440, 620, 310, 500)], served_model=QWEN)
    assert (box.x1, box.y1, box.x2, box.y2) == (0.31, 0.5, 0.44, 0.62)


def test_out_of_range_is_clamped_not_rejected() -> None:
    (box,) = to_fractions([RawBox(-40, -10, 1200, 1050)], served_model=QWEN)
    assert (box.x1, box.y1, box.x2, box.y2) == (0.0, 0.0, 1.0, 1.0)


def test_empty_input_short_circuits_before_the_model_lookup() -> None:
    # No coordinates means no convention is needed, so an unqualified model is fine.
    assert to_fractions([], served_model="unqualified-model") == []


# --- pixels -----------------------------------------------------------------


def test_pixels_scale_per_axis_on_a_portrait_photo() -> None:
    # The regression that matters: a far-from-square image must scale each axis by
    # its OWN dimension. Scaling both by the long side is the reported upstream
    # failure and would put y at 0.5*4032 = 2016 instead of 1512.
    (box,) = to_pixels([RawBox(250, 500, 750, 500)], served_model=QWEN, width=3024, height=4032)
    assert (box.x1, box.x2) == (756, 2268)
    assert box.y1 == box.y2 == 2016


def test_pixels_use_corners_not_extents() -> None:
    # bbox_2d is [x1, y1, x2, y2]. If this were read as [x, y, w, h] the box would be
    # 100x100 at (100,100) rather than 300x300 at (100,100).
    (box,) = to_pixels([RawBox(100, 100, 400, 400)], served_model=QWEN, width=1000, height=1000)
    assert (box.x1, box.y1, box.x2, box.y2) == (100, 100, 400, 400)
    assert (box.width, box.height) == (300, 300)


def test_pixels_reject_degenerate_dimensions() -> None:
    with pytest.raises(GroundingError, match="dimensions must be positive"):
        to_pixels([RawBox(1, 2, 3, 4)], served_model=QWEN, width=0, height=100)


def test_explicit_convention_overrides_the_table() -> None:
    # The probe renders one reply under both bases; that requires forcing each.
    boxes = [RawBox(0.4, 0.4, 0.6, 0.6)]
    forced = to_pixels(
        boxes,
        served_model=QWEN,
        width=1000,
        height=1000,
        convention=Convention.NORM_1000,
    )
    assert (forced[0].x1, forced[0].x2) == (0, 1)  # 0.4/1000 ≈ 0 — visibly wrong, as intended


def test_nan_is_a_parse_failure_not_a_box() -> None:
    with pytest.raises(GroundingError, match="NaN"):
        to_fractions([RawBox(float("nan"), 0.1, 0.2, 0.3)], served_model=QWEN)


# --- parsing ----------------------------------------------------------------


def test_parses_bare_json_array() -> None:
    boxes, points = parse_grounding('[{"bbox_2d": [1, 2, 3, 4], "label": "wire"}]')
    assert boxes == [RawBox(1, 2, 3, 4, label="wire")]
    assert points == []


def test_parses_fenced_json_preferring_the_fence_over_stray_prose_brackets() -> None:
    # A thinking model reasons in prose that often contains brackets before emitting
    # the real answer in a fence; the fence must win.
    text = 'I considered [the heater] and the pipe.\n```json\n[{"bbox_2d":[10,20,30,40]}]\n```'
    boxes, _ = parse_grounding(text)
    assert boxes == [RawBox(10, 20, 30, 40)]


def test_parses_json_embedded_in_prose() -> None:
    boxes, _ = parse_grounding('Here it is: [{"bbox_2d": [5, 6, 7, 8], "label": "x"}] done.')
    assert boxes == [RawBox(5, 6, 7, 8, label="x")]


def test_parses_points() -> None:
    _boxes, points = parse_grounding('[{"point_2d": [500, 250], "label": "valve"}]')
    assert len(points) == 1
    assert (points[0].x, points[0].y, points[0].label) == (500, 250, "valve")


def test_parses_multiple_boxes_preserving_order() -> None:
    boxes, _ = parse_grounding(
        '[{"bbox_2d":[1,1,2,2],"label":"a"},{"bbox_2d":[3,3,4,4],"label":"b"}]'
    )
    assert [b.label for b in boxes] == ["a", "b"]


def test_empty_result_is_an_answer_not_an_error() -> None:
    # "found nothing" must be reportable — silent undercount is the documented VLM
    # failure mode, so the caller needs to see zero rather than an exception.
    assert parse_grounding("[]") == ([], [])
    assert parse_grounding("I could not find it.") == ([], [])


def test_malformed_coordinates_are_skipped_not_fatal() -> None:
    boxes, _ = parse_grounding('[{"bbox_2d":["a","b","c","d"]},{"bbox_2d":[1,2,3,4],"label":"ok"}]')
    assert boxes == [RawBox(1, 2, 3, 4, label="ok")]


def test_wrong_arity_box_is_not_a_box() -> None:
    assert parse_grounding('[{"bbox_2d": [1, 2, 3]}]') == ([], [])


def test_alternate_key_spellings() -> None:
    boxes, _ = parse_grounding('[{"bbox": [1, 2, 3, 4], "name": "alt"}]')
    assert boxes == [RawBox(1, 2, 3, 4, label="alt")]


def test_single_object_not_wrapped_in_a_list() -> None:
    boxes, _ = parse_grounding('{"bbox_2d": [1, 2, 3, 4], "label": "solo"}')
    assert boxes == [RawBox(1, 2, 3, 4, label="solo")]
