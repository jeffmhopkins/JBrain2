"""Per-type editorial guides for the builder (the code projection of docs/WIKI_TYPE_GUIDES.md).

The guides drive the LLM rewriter: the ordered sections (each with its firewall domain) and a
per-type lead/style hint, plus the binding writing-style prompt shared by every article. A
free-text entity kind is normalized onto a guide; unknown kinds fall back to the generic guide.
Kept compact and in-code for C2b; the owner-tunable editorial-config table is a later refinement.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SectionSpec:
    name: str
    domain: str
    include_if: str


@dataclass(frozen=True)
class TypeGuide:
    type: str
    lead: str
    style: str
    sections: tuple[SectionSpec, ...]


# The binding writing-style block (docs/WIKI_TYPE_GUIDES.md "Writing style"). The hard rules
# (citation, single-domain, omit-empty) are enforced in code regardless; this steers the prose.
STYLE_PROMPT = (
    "Write a neutral, third-person, encyclopedic article, like Wikipedia. No first person — "
    "'I/my' never appears; the owner is a named entity referred to by name. Past tense for "
    "history and events, present tense for current state. First mention of the subject uses "
    "the full name; afterwards surname or pronoun. Assert only what the provided claims support "
    "— no speculation, no hedging, no invented facts; keep numbers and measurements verbatim. "
    "Cite at the smallest distinct clause (not stacked at the sentence end): every clause must "
    "be entailed by the chunk of the claim it cites. Use prose by default; a bulleted list only "
    "for 3+ short parallel non-narrative items; never invent a section not in the section plan."
)

_PERSON = TypeGuide(
    type="Person",
    lead="Who they are + their relation to the owner.",
    style="Biographical. Past tense for history, present for current state.",
    sections=(
        SectionSpec("Early life", "general", "birth, family of origin, hometown"),
        SectionSpec("Career", "general", "employers, roles, professional work"),
        SectionSpec("Personal life", "general", "relationships, residence, interests, family"),
        SectionSpec("Health", "health", "conditions, medications, allergies, providers"),
        SectionSpec("Finances", "finance", "accounts, income, obligations"),
    ),
)
_ORG = TypeGuide(
    type="Organization",
    lead="What it is + the owner's relation to it (employer, vendor, bank, club).",
    style="Factual; present tense for current structure, past for history.",
    sections=(
        SectionSpec("Overview", "general", "what the org is or does"),
        SectionSpec("History", "general", "founding, milestones"),
        SectionSpec("People", "general", "leadership or the owner's contacts there"),
        SectionSpec("Products", "general", "products or services"),
        SectionSpec("Dealings", "general", "the owner's non-financial interactions"),
        SectionSpec("Finances", "finance", "accounts, payments, contracts with the owner"),
    ),
)
_PLACE = TypeGuide(
    type="Place",
    lead="What/where it is + significance to the owner.",
    style="Descriptive.",
    sections=(
        SectionSpec("Overview", "general", "what the place is, geography"),
        SectionSpec("History", "general", "history or significance"),
        SectionSpec("Associations", "general", "who/what the owner connects to it"),
    ),
)
_GENERIC = TypeGuide(
    type="Generic",
    lead="What it is + its significance to the owner.",
    style="Neutral encyclopedic.",
    sections=(
        SectionSpec("Overview", "general", "what it is"),
        SectionSpec("Details", "general", "notable details"),
        SectionSpec("Health", "health", "health-domain facts, if any"),
        SectionSpec("Finances", "finance", "finance-domain facts, if any"),
    ),
)

# Free-text kind → guide. Mirrors the frontend's entity-kind aliasing (entities/kinds.tsx).
_ALIASES: dict[str, TypeGuide] = {
    "person": _PERSON,
    "people": _PERSON,
    "individual": _PERSON,
    "human": _PERSON,
    "patient": _PERSON,
    "organization": _ORG,
    "organisation": _ORG,
    "org": _ORG,
    "company": _ORG,
    "institution": _ORG,
    "group": _ORG,
    "team": _ORG,
    "clinic": _ORG,
    "hospital": _ORG,
    "place": _PLACE,
    "location": _PLACE,
    "city": _PLACE,
    "region": _PLACE,
    "country": _PLACE,
    "venue": _PLACE,
}


def guide_for(kind: str) -> TypeGuide:
    """Resolve a free-text entity kind to its guide; the generic guide is the safe fallback."""
    key = "".join(ch for ch in kind.lower() if ch.isalnum())
    return _ALIASES.get(key, _GENERIC)
