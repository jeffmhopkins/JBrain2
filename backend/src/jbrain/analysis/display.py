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
    renderer so the card and the entity page describe a fact identically."""
    if isinstance(value_json, dict):
        systolic, diastolic = value_json.get("systolic"), value_json.get("diastolic")
        unit = value_json.get("unit")
        if isinstance(systolic, (int, float)) and isinstance(diastolic, (int, float)):
            reading = f"{_number(systolic)}/{_number(diastolic)}"
            return f"{reading} {unit}" if isinstance(unit, str) else reading
        if "value" in value_json:
            rendered = _number(value_json["value"])
            return f"{rendered} {unit}" if isinstance(unit, str) else rendered
    return statement


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
