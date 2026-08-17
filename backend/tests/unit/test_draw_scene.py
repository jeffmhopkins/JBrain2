"""The canvas scene fold (jbrain.draw.scene) — AGENT_CANVAS_PLAN §4.

The behaviour under test is mostly about FAILURE: a batch with one bad op must still
land the other nineteen, a clamp must be reported rather than silently applied, and an
edit by id must refuse an id that isn't there instead of silently doing nothing. Those
are the things a model running at ~7 t/s cannot afford to get wrong twice.
"""

from __future__ import annotations

from typing import Any

import pytest

from jbrain.draw.scene import (
    DEFAULT_STROKE_PX,
    DEFAULT_TEXT_PX,
    MAX_ELEMENTS,
    MAX_TEXT_CHARS,
    Background,
    Scene,
    SceneError,
    apply_ops,
)


def _scene(width: int = 1000, height: int = 500) -> Scene:
    return Scene(canvas_id="cv1", background=Background(kind="blank", width=width, height=height))


def _apply(ops: list[dict[str, Any]], scene: Scene | None = None) -> tuple[Scene, Any]:
    return apply_ops(scene or _scene(), ops)


# --- the happy path ---------------------------------------------------------


def test_pixels_in_fractions_out() -> None:
    # The model speaks pixels; the scene stores fractions (§4.2).
    scene, _ = _apply([{"op": "rect", "x": 100, "y": 50, "w": 400, "h": 200}])
    assert scene.elements[0].at == {"x": 0.1, "y": 0.1, "x2": 0.5, "y2": 0.5}


def test_ids_are_sequential_and_stable_across_batches() -> None:
    scene, _ = _apply([{"op": "text", "x": 1, "y": 1, "text": "a"}])
    scene, _ = apply_ops(scene, [{"op": "text", "x": 2, "y": 2, "text": "b"}])
    assert [e.id for e in scene.elements] == ["e1", "e2"]


def test_defaults_applied() -> None:
    scene, _ = _apply([{"op": "text", "x": 10, "y": 10, "text": "hi"}])
    el = scene.elements[0]
    assert (el.size, el.stroke, el.tone, el.fill) == (
        DEFAULT_TEXT_PX,
        DEFAULT_STROKE_PX,
        "auto",
        False,
    )


def test_report_lists_what_landed() -> None:
    _scene_out, report = _apply(
        [
            {"op": "rect", "x": 1, "y": 1, "w": 5, "h": 5},
            {"op": "text", "x": 1, "y": 1, "text": "x"},
        ]
    )
    assert report.applied == ["e1 rect", "e2 text"]
    assert "applied 2 of 2" in report.line(2)


# --- partial apply: the core promise ----------------------------------------


def test_one_bad_op_does_not_lose_the_batch() -> None:
    ops: list[dict[str, Any]] = [{"op": "text", "x": i, "y": i, "text": str(i)} for i in range(5)]
    ops.insert(2, {"op": "rect", "x": 1, "y": 1})  # missing w/h
    scene, report = _apply(ops)
    assert len(scene.elements) == 5
    assert len(report.skipped) == 1
    assert "missing w" in report.skipped[0]


def test_skip_reason_names_the_op_index_and_kind() -> None:
    _s, report = _apply(
        [{"op": "text", "x": 1, "y": 1, "text": "ok"}, {"op": "arrow", "x": 1, "y": 1}]
    )
    assert report.skipped[0].startswith("op 2 (arrow) skipped:")


def test_unknown_op_lists_the_vocabulary() -> None:
    _s, report = _apply([{"op": "sparkle", "x": 1, "y": 1}])
    assert "unknown op 'sparkle'" in report.skipped[0]
    assert "label_box" in report.skipped[0]


def test_non_object_op_is_skipped_not_fatal() -> None:
    _s, report = _apply(["draw a cat", {"op": "text", "x": 1, "y": 1, "text": "ok"}])  # type: ignore[list-item]
    assert len(report.applied) == 1
    assert "expected an object" in report.skipped[0]


# --- clamping is reported, never silent -------------------------------------


def test_out_of_bounds_is_clamped_and_reported() -> None:
    scene, report = _apply([{"op": "rect", "x": 900, "y": 100, "w": 400, "h": 50}])
    assert scene.elements[0].at["x2"] == 1.0
    assert any("clamped to the canvas edge" in n for n in report.notes)


def test_negative_coordinate_is_clamped_and_reported() -> None:
    scene, report = _apply([{"op": "text", "x": -50, "y": 20, "text": "off"}])
    assert scene.elements[0].at["x"] == 0.0
    assert any("outside" in n for n in report.notes)


def test_in_bounds_draws_without_a_note() -> None:
    _s, report = _apply([{"op": "rect", "x": 10, "y": 10, "w": 10, "h": 10}])
    assert report.notes == []


def test_a_wholly_offscreen_box_is_refused_not_clamped_to_nothing() -> None:
    # Clamping a box that starts past the right edge would collapse it to zero width
    # and draw an invisible mark the model would then believe exists.
    _s, report = _apply([{"op": "rect", "x": 2000, "y": 20, "w": 100, "h": 100}])
    assert "entirely off-canvas" in report.skipped[0]


# --- per-op validation ------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "missing"),
    [
        ({"op": "text", "x": 1, "y": 1}, "text"),
        ({"op": "callout", "x": 1, "y": 1, "x2": 2, "y2": 2}, "text"),
        ({"op": "label_box", "x": 1, "y": 1, "w": 5, "h": 5}, "text"),
    ],
)
def test_text_bearing_ops_require_text(op: dict[str, Any], missing: str) -> None:
    _s, report = _apply([op])
    assert missing in report.skipped[0]


def test_zero_length_line_is_refused() -> None:
    # Would render as nothing; the model must be told rather than left believing it drew.
    _s, report = _apply([{"op": "line", "x": 10, "y": 10, "x2": 10, "y2": 10}])
    assert "same point" in report.skipped[0]


def test_non_positive_extent_is_refused() -> None:
    _s, report = _apply([{"op": "rect", "x": 10, "y": 10, "w": 0, "h": 40}])
    assert "must be positive" in report.skipped[0]


def test_non_numeric_coordinate_is_refused_with_the_field_name() -> None:
    _s, report = _apply([{"op": "text", "x": "left", "y": 10, "text": "x"}])
    assert "x must be a number" in report.skipped[0]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_coordinate_is_refused(bad: float) -> None:
    _s, report = _apply([{"op": "text", "x": bad, "y": 10, "text": "x"}])
    assert "finite" in report.skipped[0]


def test_unknown_tone_degrades_to_auto_with_a_note() -> None:
    # Degrade rather than skip: the mark is what matters, the colour is advisory.
    scene, report = _apply([{"op": "rect", "x": 1, "y": 1, "w": 9, "h": 9, "tone": "chartreuse"}])
    assert scene.elements[0].tone == "auto"
    assert any("unknown tone" in n for n in report.notes)


def test_text_is_truncated_not_rejected() -> None:
    scene, _ = _apply([{"op": "text", "x": 1, "y": 1, "text": "z" * 900}])
    assert len(scene.elements[0].text) == MAX_TEXT_CHARS


def test_sizes_are_bounded() -> None:
    scene, _ = _apply(
        [
            {"op": "text", "x": 1, "y": 1, "text": "a", "size": 9999, "width": 9999},
            {"op": "text", "x": 1, "y": 1, "text": "b", "size": -5, "width": -5},
        ]
    )
    assert scene.elements[0].size <= 200 and scene.elements[0].stroke <= 40
    assert scene.elements[1].size >= 8 and scene.elements[1].stroke >= 1


def test_boolean_is_not_silently_a_number() -> None:
    # bool is an int subclass in Python; `size: true` must fall back, not become 1px.
    scene, _ = _apply([{"op": "text", "x": 1, "y": 1, "text": "a", "size": True}])
    assert scene.elements[0].size == DEFAULT_TEXT_PX


# --- editing by id ----------------------------------------------------------


def test_delete_removes_one_mark() -> None:
    scene, _ = _apply(
        [{"op": "text", "x": 1, "y": 1, "text": "a"}, {"op": "text", "x": 2, "y": 2, "text": "b"}]
    )
    scene, report = apply_ops(scene, [{"op": "delete", "id": "e1"}])
    assert [e.id for e in scene.elements] == ["e2"]
    assert report.applied == ["deleted e1"]


def test_delete_of_a_missing_id_lists_what_exists() -> None:
    # Silently succeeding would leave the model believing it fixed the figure.
    scene, _ = _apply([{"op": "text", "x": 1, "y": 1, "text": "a"}])
    _s, report = apply_ops(scene, [{"op": "delete", "id": "e9"}])
    assert "no mark 'e9'" in report.skipped[0]
    assert "marks are: e1" in report.skipped[0]


def test_move_shifts_every_coordinate_of_the_mark() -> None:
    scene, _ = _apply([{"op": "rect", "x": 100, "y": 50, "w": 100, "h": 100}])
    scene, _ = apply_ops(scene, [{"op": "move", "id": "e1", "dx": 100, "dy": -50}])
    at = scene.elements[0].at
    assert (at["x"], at["x2"]) == pytest.approx((0.2, 0.3))
    assert (at["y"], at["y2"]) == pytest.approx((0.0, 0.2))  # y clamped at the top edge


def test_move_needs_a_direction() -> None:
    scene, _ = _apply([{"op": "text", "x": 1, "y": 1, "text": "a"}])
    _s, report = apply_ops(scene, [{"op": "move", "id": "e1"}])
    assert "non-zero dx" in report.skipped[0]


def test_ids_are_not_reused_after_a_delete() -> None:
    # Reusing e1 would make a stale reference in the model's context silently address
    # a different mark.
    scene, _ = _apply([{"op": "text", "x": 1, "y": 1, "text": "a"}])
    scene, _ = apply_ops(scene, [{"op": "delete", "id": "e1"}])
    scene, _ = apply_ops(scene, [{"op": "text", "x": 2, "y": 2, "text": "b"}])
    assert [e.id for e in scene.elements] == ["e2"]


def test_clear_empties_the_canvas_but_keeps_the_background() -> None:
    scene, _ = _apply([{"op": "rect", "x": 1, "y": 1, "w": 9, "h": 9}])
    scene, _ = apply_ops(scene, [{"op": "clear"}])
    assert scene.elements == ()
    assert scene.background.width == 1000


def test_element_cap_refuses_further_marks() -> None:
    scene = _scene()
    scene, _ = apply_ops(scene, [{"op": "text", "x": 1, "y": 1, "text": "x"}] * MAX_ELEMENTS)
    _s, report = apply_ops(scene, [{"op": "text", "x": 1, "y": 1, "text": "over"}])
    assert f"already holds {MAX_ELEMENTS} marks" in report.skipped[0]


# --- round-trip + summary ---------------------------------------------------


def test_scene_round_trips_through_json_shape() -> None:
    scene, _ = _apply(
        [
            {"op": "label_box", "x": 10, "y": 20, "w": 30, "h": 40, "text": "hi", "tone": "info"},
            {"op": "callout", "x": 1, "y": 2, "x2": 3, "y2": 4, "text": "there"},
        ]
    )
    assert Scene.from_dict(scene.to_dict()) == scene


def test_from_dict_rejects_a_corrupt_scene() -> None:
    with pytest.raises(SceneError):
        Scene.from_dict({"canvas_id": "cv1"})  # no background


def test_from_dict_rejects_a_corrupt_element() -> None:
    with pytest.raises(SceneError):
        Scene.from_dict(
            {
                "canvas_id": "cv1",
                "background": {"kind": "blank", "width": 10, "height": 10, "ref": None},
                "elements": [{"id": "e1"}],
            }
        )


def test_summary_is_in_pixels_so_it_feeds_the_next_op() -> None:
    # The model must be able to copy a number straight out of the summary into its
    # next op without doing arithmetic on fractions.
    scene, _ = _apply([{"op": "rect", "x": 100, "y": 50, "w": 400, "h": 200, "text": ""}])
    assert scene.summary() == "e1 rect(100,50 400x200)"


def test_summary_of_an_empty_canvas() -> None:
    assert _scene().summary() == "no marks yet"


# --- defaults scale with the canvas ----------------------------------------


def test_defaults_scale_up_on_a_large_photo() -> None:
    # Observed live: the first real annotation put correct boxes on a 4080px phone
    # photo, but a 3px stroke on a 4080px canvas renders as a hairline — the boxes were
    # right and nearly invisible. Defaults are proportions now, not absolutes.
    scene, _ = _apply(
        [{"op": "label_box", "x": 10, "y": 10, "w": 200, "h": 200, "text": "bottle"}],
        _scene(4080, 3072),
    )
    assert scene.elements[0].stroke == 10
    assert scene.elements[0].size == 49


def test_a_small_sheet_is_unchanged() -> None:
    # The scaling must be a no-op at the size the floors were tuned for, so an existing
    # blank-canvas figure renders byte-identically.
    scene, _ = _apply([{"op": "rect", "x": 10, "y": 10, "w": 50, "h": 50}], _scene(1024, 768))
    assert (scene.elements[0].stroke, scene.elements[0].size) == (
        DEFAULT_STROKE_PX,
        DEFAULT_TEXT_PX,
    )


def test_an_explicit_size_always_wins() -> None:
    # Scaling decides what an OMITTED value means; it must never override the model.
    scene, _ = _apply(
        [{"op": "text", "x": 10, "y": 10, "text": "x", "size": 20, "width": 2}],
        _scene(4080, 3072),
    )
    assert (scene.elements[0].size, scene.elements[0].stroke) == (20, 2)
