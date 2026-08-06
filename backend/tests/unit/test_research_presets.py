"""Unit tests for the report-preset loader + strict renderer (research_presets.py)."""

from __future__ import annotations

import pytest

from jbrain.agent import research_presets as rp


def test_shipped_candidate_profile_loads_and_declares_its_variables() -> None:
    assert "candidate_profile" in rp.available()
    preset = rp.get("candidate_profile")
    assert preset is not None
    # The two slots the template uses are discovered from the text, in sorted order.
    assert preset.variables == ("candidate", "office")
    assert preset.sections[0] == "Summary"
    assert len(preset.angles) == 5


def test_render_substitutes_every_slot_across_all_fields() -> None:
    r = rp.render_preset(
        "candidate_profile", {"candidate": "Jane Doe", "office": "U.S. Senate (Florida)"}
    )
    assert r.question == "Candidate profile — Jane Doe for U.S. Senate (Florida)"
    assert "Jane Doe" in r.objective and "U.S. Senate (Florida)" in r.objective
    # No unfilled slots leak into any rendered string.
    for text in (r.question, r.objective, *r.sections, *(b for _, b in r.angles)):
        assert "{{" not in text and "}}" not in text
    # Angles keep the (title, brief) shape the planner's sub_questions use.
    assert all(isinstance(t, str) and isinstance(b, str) for t, b in r.angles)


def test_missing_variable_is_a_strict_refusal_naming_the_field() -> None:
    with pytest.raises(rp.PresetError) as exc:
        rp.render_preset("candidate_profile", {"candidate": "Jane Doe"})  # no office
    assert "office" in str(exc.value)


def test_unknown_preset_refuses_and_lists_available() -> None:
    with pytest.raises(rp.PresetError) as exc:
        rp.render_preset("does_not_exist", {})
    assert "candidate_profile" in str(exc.value)


def test_coerce_rejects_missing_sections_and_angles() -> None:
    with pytest.raises(rp.PresetError):
        rp._coerce_preset("bad", {"question": "q", "angles": [{"brief": "b"}]})  # no sections
    with pytest.raises(rp.PresetError):
        rp._coerce_preset("bad", {"question": "q", "sections": ["S"]})  # no angles
    with pytest.raises(rp.PresetError):
        rp._coerce_preset("bad", {"sections": ["S"], "angles": [{"brief": "b"}]})  # no question


def test_coerce_defaults_and_angle_title_fallback() -> None:
    preset = rp._coerce_preset(
        "p",
        {
            "question": "Profile {{x}}",
            "sections": ["One"],
            "angles": [{"brief": "research {{x}}"}],  # no title → derived from brief
        },
    )
    assert preset.output_kind == "report"  # default
    assert preset.source_mode == "web"  # default
    assert preset.variables == ("x",)
    assert preset.angles[0][0] == "research {{x}}"  # title falls back to the brief
