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


def test_shipped_daily_news_runs_on_the_briefing_engine() -> None:
    # daily_news now runs the lean deterministic-gather → single-writer `briefing` engine
    # (DAILY_NEWS_V2_PLAN.md, V3 — the v2 twin was promoted to the default and the fan pipeline
    # retired for this preset). It does NOT set news_feeds/min_reads: the builder gathers by fixed
    # beats itself, so those are pipeline-only knobs and are intentionally absent.
    #
    # Its variables — `today` and `now` — are both auto-supplied by the engine at run time
    # (owner-local), so it stays a one-call preset for the caller. `today` dates the question
    # (keeping each day a distinct dedup row); `now` carries the time of day for the "last 24 hours
    # up to now" window. It opts into a 7-day TTL (REPORT_EXPIRY_PLAN.md).
    assert "daily_news" in rp.available()
    preset = rp.get("daily_news")
    assert preset is not None
    assert preset.engine == "briefing"
    assert preset.variables == ("now", "today")
    assert preset.retention_days == 7
    assert preset.news_feeds == () and preset.min_reads is None  # builder gathers itself
    assert preset.output_kind == "brief"  # `report` would balloon past a 10-minute read
    assert preset.source_mode == "web"
    assert preset.sections[0] == "Good Morning"
    assert preset.sections[-1] == "That's Your Briefing"
    vars0 = {"today": "Friday, August 09, 2026", "now": "Friday, August 09, 2026 at 6:15 AM EDT"}
    r = rp.render_preset("daily_news", vars0)
    assert "Friday, August 09, 2026" in r.question
    assert r.engine == "briefing"  # the engine field survives render
    for text in (r.question, r.objective, *r.sections, *(b for _, b in r.angles)):
        assert "{{" not in text and "}}" not in text
    # Two different run dates yield different questions → distinct (question_hash) rows, so a
    # week of briefings coexists instead of upserting in place.
    other = rp.render_preset("daily_news", {**vars0, "today": "Saturday, August 10, 2026"})
    assert other.question != r.question


def test_render_substitutes_every_slot_across_all_fields() -> None:
    r = rp.render_preset(
        "candidate_profile", {"candidate": "Jane Doe", "office": "U.S. Senate (Florida)"}
    )
    assert r.question == "Candidate accountability profile — Jane Doe for U.S. Senate (Florida)"
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
    assert preset.engine == "pipeline"  # default engine (the fan pipeline)
    assert preset.variables == ("x",)
    assert preset.angles[0][0] == "research {{x}}"  # title falls back to the brief
    assert preset.retention_days is None  # absent → keep forever
    assert preset.news_feeds == ()  # absent → no feed pre-pull
    assert preset.records_subject == ""  # absent → no public-records pre-gather
    assert preset.lean_tail is False  # absent → the full critique→revise tail


def test_retention_days_parsed_and_carried_through_render(monkeypatch: pytest.MonkeyPatch) -> None:
    preset = rp._coerce_preset(
        "ephemeral",
        {
            "question": "News {{x}}",
            "sections": ["One"],
            "angles": [{"brief": "gather {{x}}"}],
            "retention_days": 7,
        },
    )
    assert preset.retention_days == 7
    # Register it so render_preset resolves it, and confirm the scalar TTL survives render
    # (it carries no `{{var}}`, so it passes straight through to the engine).
    monkeypatch.setitem(rp._PRESETS, "ephemeral", preset)
    assert rp.render_preset("ephemeral", {"x": "today"}).retention_days == 7
    # A preset without the field renders as keep-forever (None), the default path unchanged.
    keep = rp.render_preset("candidate_profile", {"candidate": "A", "office": "B"})
    assert keep.retention_days is None


def test_news_feeds_parses_a_list_and_defaults_empty() -> None:
    preset = rp._coerce_preset(
        "p",
        {
            "question": "News {{x}}",
            "sections": ["One"],
            "angles": [{"brief": "gather {{x}}"}],
            "min_reads": 8,  # the pre-pull only runs on the two-phase gather
            "news_feeds": ["space", " local "],  # trimmed
        },
    )
    assert preset.news_feeds == ("space", "local")
    # Absent → empty (feature off), carried through render unchanged.
    bare = rp._coerce_preset("p", {"question": "q", "sections": ["S"], "angles": [{"brief": "b"}]})
    assert bare.news_feeds == ()


def test_shipped_candidate_profile_v2_opts_into_the_records_pregather_and_lean_tail() -> None:
    # The A/B twin (CANDIDATE_PROFILE_V2_PLAN.md): same product + variables as candidate_profile,
    # but it turns on the deterministic public-records pre-gather (subject = the {{candidate}}) and
    # the lean write tail. v1 stays byte-unchanged (no such fields).
    assert "candidate_profile_v2" in rp.available()
    v2 = rp.get("candidate_profile_v2")
    v1 = rp.get("candidate_profile")
    assert v2 is not None and v1 is not None
    assert v2.records_subject == "{{candidate}}"
    assert v2.lean_tail is True
    assert v2.variables == v1.variables == ("candidate", "office")  # same caller contract
    assert v2.sections == v1.sections  # same product shape
    # v1 is untouched by the new policies.
    assert v1.records_subject == "" and v1.lean_tail is False
    # The subject template renders to the run's candidate (a real {{var}}, not a scalar).
    r = rp.render_preset(
        "candidate_profile_v2", {"candidate": "Jane Q. Doe", "office": "U.S. Senate (Florida)"}
    )
    assert r.records_subject == "Jane Q. Doe" and r.lean_tail is True


def test_records_subject_requires_web_sources() -> None:
    # The public-records pre-gather grounds an OPEN-WEB report; on a library/reports preset it would
    # be a silent no-op, so it is a load-time refusal.
    with pytest.raises(rp.PresetError) as exc:
        rp._coerce_preset(
            "p",
            {
                "question": "Profile {{x}}",
                "sections": ["S"],
                "angles": [{"brief": "b {{x}}"}],
                "sources": "reports",
                "records_subject": "{{x}}",
            },
        )
    assert "records_subject" in str(exc.value) and "web" in str(exc.value)


def test_records_subject_slots_join_the_required_variables() -> None:
    # A {{var}} used ONLY in records_subject must still be a declared, required variable, so a run
    # missing it is refused rather than rendering a half-filled subject.
    preset = rp._coerce_preset(
        "p",
        {
            "question": "Profile someone",
            "sections": ["S"],
            "angles": [{"brief": "gather"}],
            "records_subject": "{{subject}}",
        },
    )
    assert "subject" in preset.variables


@pytest.mark.parametrize("bad", ["true", 1, "yes"])
def test_lean_tail_rejects_a_non_boolean(bad: object) -> None:
    with pytest.raises(rp.PresetError) as exc:
        rp._coerce_preset(
            "p",
            {
                "question": "q",
                "sections": ["S"],
                "angles": [{"brief": "b"}],
                "lean_tail": bad,
            },
        )
    assert "lean_tail" in str(exc.value)


def test_news_feeds_requires_min_reads() -> None:
    # The feed pre-pull only runs on the two-phase (min_reads) gather, so news_feeds without
    # min_reads would be a silent no-op — a load-time refusal, not a rotting field.
    with pytest.raises(rp.PresetError) as exc:
        rp._coerce_preset(
            "p",
            {
                "question": "q",
                "sections": ["S"],
                "angles": [{"brief": "b"}],
                "news_feeds": ["space"],  # no min_reads
            },
        )
    assert "news_feeds" in str(exc.value) and "min_reads" in str(exc.value)


@pytest.mark.parametrize("bad", ["space", [""], ["ok", 3], [None]])
def test_news_feeds_rejects_a_non_string_list(bad: object) -> None:
    with pytest.raises(rp.PresetError) as exc:
        rp._coerce_preset(
            "p",
            {
                "question": "q",
                "sections": ["S"],
                "angles": [{"brief": "b"}],
                "news_feeds": bad,
            },
        )
    assert "news_feeds" in str(exc.value)


@pytest.mark.parametrize("bad", [0, -1, 3.5, True, "7", "seven"])
def test_retention_days_rejects_non_positive_int(bad: object) -> None:
    with pytest.raises(rp.PresetError) as exc:
        rp._coerce_preset(
            "p",
            {
                "question": "q",
                "sections": ["S"],
                "angles": [{"brief": "b"}],
                "retention_days": bad,
            },
        )
    assert "retention_days" in str(exc.value)
