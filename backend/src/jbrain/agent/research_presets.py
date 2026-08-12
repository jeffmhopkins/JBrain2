"""Saved, parameterized report PRESETS for the deep-research engine
(docs/plans/REPORT_PRESET_PLAN.md).

A preset is a checked-in template that pins a report's SHAPE — its section outline and
the gather angles the research children work — so the same structure comes out for every
subject you point it at (every candidate on a ballot, every product in a bake-off). It is
the saved, variable-filled twin of the dict the planner (`deep_research._plan`) normally
invents fresh each run: supply it and the engine skips planning and runs the fixed plan;
omit it and the run self-orchestrates exactly as before (presets are strictly opt-in, the
default path is byte-unchanged).

A preset file is pure YAML (`*.preset`) with `{{ variable }}` slots — the SAME mustache
the prompt files use — filled per run from a `variables` dict. Rendering is strict: a
template that names a variable the caller didn't supply is a clean refusal, never a blank
substitution, so a half-filled report can't ship. Structure is validated at load
(startup), like every prompt file, so a malformed preset fails fast rather than mid-run.

This module is deliberately free of any `deep_research` import (the dependency runs one
way — `deep_research` reads presets), so the value allowlists it can't see here
(`output_kind`, `source_mode`) are validated by the engine when it consumes a rendered
preset, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml

log = structlog.get_logger()

_PRESETS_DIR = Path(__file__).parent / "presets"

# The `{{ name }}` slot — the same shape `llm/promptfile.py` substitutes, so a preset author
# reuses the one templating convention the repo already has.
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class PresetError(ValueError):
    """A preset file is malformed (load time), or a render is missing a variable / names an
    unknown preset (call time). Surfaced to the caller as a clean refusal, never a crash."""


@dataclass(frozen=True)
class Preset:
    """One saved report template, as authored — every string still carries its `{{ var }}`
    slots. `variables` is the union of slots used across all templates, so a render can check
    the caller supplied them all before any substitution runs."""

    name: str
    description: str
    question: str
    objective: str
    output_kind: str
    source_mode: str
    sections: tuple[str, ...]
    angles: tuple[tuple[str, str], ...]  # (title, brief)
    variables: tuple[str, ...]
    # Optional TTL (REPORT_EXPIRY_PLAN.md): N days after which the nightly expiry sweep reaps
    # the persisted report. None = keep forever (the default; candidate_profile omits it,
    # daily_news sets 7). A scalar policy, not a `{{var}}` slot.
    retention_days: int | None
    # Optional gather READ floor: the fewest web pages the run must actually OPEN (web_fetch,
    # not just search-list) before it writes. When gather comes back under it, the engine
    # force-fetches the top unopened results via the fetch-only persona so the writer cites
    # real page text, not snippets (the daily_news fetch-light failure). None = no floor (the
    # default; every existing preset omits it). A scalar policy, not a `{{var}}` slot.
    min_reads: int | None
    # Optional curated-feed categories to PRE-PULL as full-text findings (NEWS_FEED_PLAN.md
    # Wave B): before the two-phase gather runs, the engine pulls these `news_feed` categories
    # and injects each full-text article (the feeds that carry `content:encoded`) straight in as
    # a gather finding, so the walled/slow space+local angles skip the reader fetch entirely.
    # Empty = off (the default; only a min_reads news brief opts in). A scalar policy, not a
    # `{{var}}` slot.
    news_feeds: tuple[str, ...]


@dataclass(frozen=True)
class RenderedPreset:
    """A preset with every `{{ var }}` filled from a run's variables — the concrete plan the
    engine runs. `angles` maps onto the planner's `sub_questions` (title, brief) shape and
    `sections` onto its `sections`, so the engine consumes it exactly where `_plan`'s output
    would go."""

    name: str
    question: str
    objective: str
    output_kind: str
    source_mode: str
    sections: tuple[str, ...]
    angles: tuple[tuple[str, str], ...]
    # The preset's TTL, passed straight through render (it carries no `{{var}}`), so the engine
    # stamps `expires_at` on the persisted report. None = keep forever.
    retention_days: int | None
    # The preset's gather read floor, passed straight through render. None = no floor.
    min_reads: int | None
    # The preset's curated-feed pre-pull categories, passed straight through render. () = off.
    news_feeds: tuple[str, ...]


def _slots(text: str) -> set[str]:
    return {m.group(1) for m in _VAR.finditer(text)}


def _render(text: str, variables: dict[str, str], *, where: str) -> str:
    """Substitute every `{{ var }}` in `text`. A slot with no matching variable is a
    PresetError (strict, like promptfile.render) — a half-filled report is worse than a
    clean refusal, and `where` names the field so the error is actionable."""
    missing: set[str] = set()

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in variables:
            missing.add(name)
            return m.group(0)
        return str(variables[name])

    out = _VAR.sub(sub, text)
    if missing:
        raise PresetError(f"{where}: missing variable(s) {sorted(missing)}")
    return out


def _pos_int_field(preset: str, field: str, value: object) -> int | None:
    """A scalar preset policy that is either absent (None) or a positive integer. A bool is an
    int subclass, so exclude it explicitly; anything non-positive or non-int is a load-time
    refusal (fail fast at startup) rather than a silently dropped policy."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise PresetError(f"preset {preset!r}: `{field}` must be a positive integer")


def _str_list_field(preset: str, field: str, value: object) -> tuple[str, ...]:
    """A scalar preset policy that is either absent (empty tuple) or a list of non-empty
    strings — e.g. `news_feeds: [space, local]`. Anything else (a bare string, a list with a
    non-string / blank item) is a load-time refusal (fail fast at startup)."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PresetError(f"preset {preset!r}: `{field}` must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PresetError(f"preset {preset!r}: `{field}` items must be non-empty strings")
        out.append(item.strip())
    return tuple(out)


def _coerce_preset(name: str, raw: object) -> Preset:
    """Validate one loaded YAML doc into a Preset, or raise PresetError. Structure only —
    the `output_kind` / `source_mode` value allowlists live in the engine (no import cycle),
    which refuses an unknown value when it consumes the rendered preset."""
    if not isinstance(raw, dict):
        raise PresetError(f"preset {name!r}: top level must be a mapping")
    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        raise PresetError(f"preset {name!r}: a non-empty `question` template is required")
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        raise PresetError(f"preset {name!r}: `sections` must be a non-empty list of headings")
    sections = tuple(str(s).strip() for s in sections_raw if str(s).strip())
    if not sections:
        raise PresetError(f"preset {name!r}: `sections` has no non-empty heading")
    angles_raw = raw.get("angles")
    if not isinstance(angles_raw, list) or not angles_raw:
        raise PresetError(f"preset {name!r}: `angles` must be a non-empty list of research briefs")
    angles: list[tuple[str, str]] = []
    for i, a in enumerate(angles_raw):
        if not isinstance(a, dict):
            raise PresetError(f"preset {name!r}: angle {i} must be a mapping with a `brief`")
        brief = a.get("brief")
        if not isinstance(brief, str) or not brief.strip():
            raise PresetError(f"preset {name!r}: angle {i} needs a non-empty `brief`")
        title = a.get("title")
        title = title.strip() if isinstance(title, str) and title.strip() else brief.strip()
        angles.append((title, brief.strip()))
    objective = raw.get("objective") or ""
    if not isinstance(objective, str):
        raise PresetError(f"preset {name!r}: `objective` must be a string")
    output_kind = str(raw.get("output_kind") or "report")
    source_mode = str(raw.get("sources") or "web")
    # Optional scalar policies. Absent → None (the feature is off). Present → a positive int; a
    # bool, non-int, or non-positive value is a load-time refusal (fail fast at startup, like
    # every other malformed field) rather than a silently ignored policy.
    #   retention_days — the persisted report's TTL in days (REPORT_EXPIRY_PLAN.md).
    #   min_reads      — the gather read floor: the fewest pages the run must OPEN before it
    #                    writes (the engine force-fetches to reach it — REPORT_PRESET_PLAN.md).
    retention_days = _pos_int_field(name, "retention_days", raw.get("retention_days"))
    min_reads = _pos_int_field(name, "min_reads", raw.get("min_reads"))
    #   news_feeds — curated `news_feed` categories to pre-pull as full-text findings (Wave B).
    news_feeds = _str_list_field(name, "news_feeds", raw.get("news_feeds"))
    # The feed pre-pull only runs on the two-phase (min_reads) gather, so `news_feeds` without
    # `min_reads` would be a silent no-op — refuse it at load rather than let it rot unnoticed.
    if news_feeds and min_reads is None:
        raise PresetError(
            f"preset {name!r}: `news_feeds` requires `min_reads` (the feed pre-pull runs only on "
            "the two-phase scout→read gather, so it is inert without a read target)"
        )
    # The variables the caller must supply = every slot used anywhere in the template.
    used: set[str] = set()
    for text in (question, objective, *sections, *(b for _, b in angles), *(t for t, _ in angles)):
        used |= _slots(text)
    return Preset(
        name=name,
        description=str(raw.get("description") or ""),
        question=question.strip(),
        objective=objective.strip(),
        output_kind=output_kind,
        source_mode=source_mode,
        sections=sections,
        angles=tuple(angles),
        variables=tuple(sorted(used)),
        retention_days=retention_days,
        min_reads=min_reads,
        news_feeds=news_feeds,
    )


def _load_all() -> dict[str, Preset]:
    """Load every `*.preset` file in the presets dir at import, keyed by file stem. A
    malformed file raises at import (startup), so a broken preset never reaches a run."""
    out: dict[str, Preset] = {}
    if not _PRESETS_DIR.is_dir():
        return out
    for path in sorted(_PRESETS_DIR.glob("*.preset")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        preset = _coerce_preset(path.stem, raw)
        out[preset.name] = preset
    return out


_PRESETS: dict[str, Preset] = _load_all()


def available() -> list[str]:
    """The names of every loaded preset, for an unknown-preset refusal message."""
    return sorted(_PRESETS)


def get(name: str) -> Preset | None:
    return _PRESETS.get(name)


def render_preset(name: str, variables: dict[str, str]) -> RenderedPreset:
    """Fill a preset's `{{ var }}` slots from `variables`. Raises PresetError for an unknown
    preset or a missing variable (both caller-correctable refusals)."""
    preset = _PRESETS.get(name)
    if preset is None:
        raise PresetError(f"unknown preset {name!r}; available: {available()}")
    v = variables or {}
    return RenderedPreset(
        name=preset.name,
        question=_render(preset.question, v, where=f"preset {name!r} question"),
        objective=_render(preset.objective, v, where=f"preset {name!r} objective")
        if preset.objective
        else "",
        output_kind=preset.output_kind,
        source_mode=preset.source_mode,
        sections=tuple(
            _render(s, v, where=f"preset {name!r} section {i}")
            for i, s in enumerate(preset.sections)
        ),
        angles=tuple(
            (
                _render(t, v, where=f"preset {name!r} angle {i} title"),
                _render(b, v, where=f"preset {name!r} angle {i} brief"),
            )
            for i, (t, b) in enumerate(preset.angles)
        ),
        retention_days=preset.retention_days,
        min_reads=preset.min_reads,
        news_feeds=preset.news_feeds,
    )
