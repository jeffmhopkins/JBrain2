"""Display fields the analysis surfaces render verbatim.

Snippets carry literal <mark>…</mark> around the cited span — the same
convention search headlines use — and the frontend splits on the tags, so a
snippet with no anchored span is simply served unmarked.

Review payloads carry the precomputed card fields (`summary`, `snippet`,
`outcomes` {accept, reject}, `choices` [{action, label, detail?,
destructive?}]) alongside the row ids the resolution handlers read. Two
invariants: every advertised choice action and outcome verb is exactly an
action POST /review/{id}/resolve accepts, and the wording stays in the UI's
lowercase-calm register (frontend mock.ts is the reference fixtures).
"""

from collections.abc import Sequence
from typing import Any

SNIPPET_CHARS = 240
# Context kept before a span that sits deeper in the chunk than the window.
_LEAD_CHARS = 60


def mark_snippet(text: str | None, start: int | None = None, end: int | None = None) -> str | None:
    """Window `text` around [start, end) and wrap that span in <mark>.

    A missing or degenerate span (zero-width paraphrase anchors, out-of-range
    offsets) serves the plain head of the text — unmarked, never mismarked.
    """
    if text is None:
        return None
    if start is None or end is None or not (0 <= start < end <= len(text)):
        return text[:SNIPPET_CHARS]
    lead = 0 if end <= SNIPPET_CHARS else max(0, start - _LEAD_CHARS)
    window = text[lead : lead + SNIPPET_CHARS]
    span_start, span_end = start - lead, end - lead
    if span_end > len(window):  # span longer than the window itself
        return window
    return f"{window[:span_start]}<mark>{window[span_start:span_end]}</mark>{window[span_end:]}"


def _number(value: Any) -> str:
    # 128 stays "128" whether it arrived as int or float.
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def value_label(value_json: dict[str, Any] | None, statement: str) -> str:
    """Plain-language value for a choice button — mirrors the UI's factValue
    renderer so the card and the entity page describe a fact identically.

    Renders the bare datum from value_json (a recognized shape, else the first
    string leaf of an unhandled shape), and falls back to the statement when
    value_json carries no datum. NEVER empty: a choice button / value cell must
    always show something, so the statement is the floor (the note.extract prompt
    is what keeps value_json a bare datum; this only renders what is stored)."""
    return _structured_label(value_json) or statement


def _structured_label(value_json: dict[str, Any] | None) -> str | None:
    """The bare datum a value_json reduces to, or None when it carries none."""
    if not isinstance(value_json, dict):
        return None
    systolic, diastolic = value_json.get("systolic"), value_json.get("diastolic")
    unit = value_json.get("unit")
    if isinstance(systolic, (int, float)) and isinstance(diastolic, (int, float)):
        reading = f"{_number(systolic)}/{_number(diastolic)}"
        return f"{reading} {unit}" if isinstance(unit, str) else reading
    if "value" in value_json:
        rendered = _number(value_json["value"])
        return f"{rendered} {unit}" if isinstance(unit, str) else rendered
    # Any other single-datum shape ({"name": …}, {"place": …}, {"street": …}): the
    # first non-empty STRING leaf is the datum. A date shape ({"start": ISO}) is
    # left to the statement — there is no date formatter here, and the entity page
    # (format.ts) renders {start} via fmtTemporal — so the start/end keys are
    # skipped rather than surfaced as a raw ISO timestamp.
    for key, v in value_json.items():
        if key in ("start", "end"):
            continue
        if isinstance(v, str) and v.strip():
            return v
    return None


def collision_display(
    *,
    kind: str,
    predicate: str,
    entity_ref: str,
    changed: bool,
    label_a: str,
    label_b: str,
    snippet: str | None,
) -> dict[str, Any]:
    """attribute_collision / fact_conflict card fields: a human picks a side,
    so the resolution lives in the choices — no generic accept/reject verbs
    are advertised (the card hides its footer verbs accordingly)."""
    if kind == "attribute_collision":
        summary = f"two values recorded for {entity_ref}'s {predicate}"
    elif changed:
        summary = f"{entity_ref}'s {predicate} changed"
    else:
        summary = f"two {predicate} values disagree for {entity_ref}"
    return {
        "summary": summary,
        "snippet": snippet,
        "choices": [
            {"action": "accept_a", "label": label_a, "detail": "previously recorded"},
            {"action": "accept_b", "label": label_b, "detail": "from this note"},
        ],
    }


def promotion_display(
    *, predicate: str, proposed: str, note_domain: str, snippet: str | None
) -> dict[str, Any]:
    return {
        "summary": f"this {predicate} fact may belong in {proposed}, not {note_domain}",
        "snippet": snippet,
        "outcomes": {
            "accept": f"the fact moves to {proposed} and is pinned there —"
            " reprocessing can't pull it back.",
            "reject": f"the fact stays in {note_domain} — the note's firewall keeps it.",
        },
    }


def merge_display(*, keep_name: str, gone_name: str, snippet: str | None) -> dict[str, Any]:
    """merge_proposal card fields. accept folds `gone` into `keep`; reject
    writes the permanent distinct_from edge — both are POST /resolve verbs, so
    the generic accept/reject footer carries them."""
    return {
        "summary": f"are {gone_name} and {keep_name} the same?",
        "snippet": snippet,
        "outcomes": {
            "accept": f"{gone_name} merges into {keep_name} — their mentions and facts combine.",
            "reject": f"{keep_name} and {gone_name} stay separate — never re-proposed.",
        },
    }


def truncation_display(*, kept: int, dropped: int, snippet: str | None) -> dict[str, Any]:
    """extraction_truncated card fields: an informational notice that this note
    held more durable facts than its per-note budget, so the tail was clipped.
    Like ambiguous_mention it writes no graph state, so its only verb (reject)
    is a dismissal — the owner re-runs with a larger budget to capture more."""
    noun = "fact" if dropped == 1 else "facts"
    return {
        "summary": f"this note hit its fact budget — kept {kept}, skipped {dropped} {noun}",
        "snippet": snippet,
        "outcomes": {
            "reject": "the note is left as-is — re-run analysis to capture more of it.",
        },
    }


def ambiguous_display(*, name: str, snippet: str | None) -> dict[str, Any]:
    """ambiguous_mention card fields. accept is deliberately not advertised:
    linking a specific candidate needs the layer-2/3 resolution machinery."""
    return {
        "summary": f"which {name}?",
        "snippet": snippet,
        "outcomes": {
            "reject": "the mention stays unlinked — it can be re-proposed with more signal.",
        },
    }


def inference_display(*, statement: str, reasons: list[str], snippet: str | None) -> dict[str, Any]:
    """low_confidence_inference card fields: a held fact (cross-subject, ambiguous,
    or below the commit threshold) written as a pending_review row. accept pins it
    active and durable through reprocessing; reject retracts it. Both are POST
    /resolve verbs carried by the generic accept/reject footer."""
    why = ", ".join(reasons) or "low confidence"
    return {
        "summary": f"hold for review ({why}): {statement}",
        "snippet": snippet,
        "outcomes": {
            "accept": "the fact is recorded and pinned — reprocessing won't drop it.",
            "reject": "the fact is discarded.",
        },
    }


def confirm_entity_display(*, name: str, kind: str, snippet: str | None = None) -> dict[str, Any]:
    """confirm_entity card fields: an entity crossed the corroboration bar but its
    identity is contested (a live namesake), so promotion is held for a human/agent
    rather than auto-cementing a possibly-wrong identity. accept confirms it;
    reject leaves it provisional. Both ride the generic accept/reject footer."""
    return {
        "summary": f"is this {kind.lower()} “{name}” a single, confirmed entity?",
        "snippet": snippet,
        "outcomes": {
            "accept": "the entity is confirmed — it survives note deletion and isn't auto-purged.",
            "reject": "left provisional — it stays purge-eligible and is never re-proposed.",
        },
    }


def new_predicate_display(
    *, predicate: str, suggestions: Sequence[tuple[str, float]], snippet: str | None = None
) -> dict[str, Any]:
    """new_predicate card fields: an unknown predicate the canonicalizer could not
    confidently merge (Phase 3 §3.1a). The fact already committed under its raw
    name; the card is suggestion-led — map it onto a nearby canonical, keep it as
    a new one, or dismiss. Each advertised action is one /resolve accepts
    (map_to_existing carries the chosen canonical_name; suggest_better is the same
    control as accept with the name field edited, so it has no static choice)."""
    near = ", ".join(f"{name} ({sim:.2f})" for name, sim in suggestions[:3])
    hint = f" — nearest: {near}" if near else " — no close match"
    choices: list[dict[str, Any]] = [
        {
            "action": "map_to_existing",
            "label": name,
            "detail": f"map onto {name} (≈ {sim:.2f})",
            "canonical_name": name,
        }
        for name, sim in suggestions[:3]
    ]
    choices.append({"action": "accept_as_new", "label": f"keep '{predicate}' as a new predicate"})
    choices.append({"action": "reject", "label": "dismiss", "detail": "leave it as-is"})
    return {
        "summary": f"new predicate '{predicate}'{hint}",
        "snippet": snippet,
        "choices": choices,
    }
