"""Map a natural-language relationship word to the graph predicate(s) it names.

A user asks "my wife", "my boss", "my mom"; the graph stores those bonds under
canonical predicates (`spouse`, `reportsTo`, `parent`) the extractor steers
toward (see `note_extract.prompt` and the reciprocity registries in
`supersession.py`). The schema's `renamed_from` normalizes drift *spellings*
(`legalName` → `name.legal`) but never natural-language *relationship words* —
"wife" is not a predicate, so a name search for it finds nothing. This module is
the query-time bridge the extractor's `renamed_from` is not: it turns a
relationship word into the set of predicate spellings to match against an
entity's outbound edges.

Directionality matters. `relate` follows an anchor entity's OUTBOUND edges to the
object on the other side, so each word maps to the predicate spelled on the
anchor's own edge: "my boss" is `Me.reportsTo → boss` (not `manages`), "my
landlord" is `Me.tenant_of → landlord` (not `landlord_of`). Both the schema.org
and snake_case spellings are listed because either may be stored, and the match
is case-insensitive (compared against `lower(predicate)`).
"""

from __future__ import annotations

import re

_SEP = re.compile(r"[\s_]+")

# Relationship word → the predicate spellings that, as the anchor's OUTBOUND
# edge, point at that relation. Lowercased; both schema.org and snake_case twins
# listed so a match lands whichever the extractor emitted. Keys include the
# common synonyms and plurals the owner actually types.
_RELATIONSHIP_WORDS: dict[str, tuple[str, ...]] = {
    # Spouse is symmetric and functional; gendered words still mean `spouse`,
    # the only predicate stored.
    "spouse": ("spouse", "married_to", "marriedto"),
    "wife": ("spouse", "married_to", "marriedto"),
    "husband": ("spouse", "married_to", "marriedto"),
    "partner": ("spouse", "married_to", "marriedto"),
    "fiance": ("engaged_to", "engagedto", "spouse"),
    "fiancee": ("engaged_to", "engagedto", "spouse"),
    # Parents: the owner's edge to a parent is `parent`.
    "parent": ("parent", "parent_of", "parentof"),
    "mom": ("parent", "parent_of", "parentof"),
    "mother": ("parent", "parent_of", "parentof"),
    "mum": ("parent", "parent_of", "parentof"),
    "dad": ("parent", "parent_of", "parentof"),
    "father": ("parent", "parent_of", "parentof"),
    # Children: the owner's edge to a kid is `children` (or a bare `child`).
    "child": ("children", "child"),
    "kid": ("children", "child"),
    "son": ("children", "child"),
    "daughter": ("children", "child"),
    # Siblings are symmetric; twin-ness rides a qualifier but a bare `twin`
    # predicate is tolerated.
    "sibling": ("sibling", "sibling_of", "siblingof"),
    "brother": ("sibling", "sibling_of", "siblingof"),
    "sister": ("sibling", "sibling_of", "siblingof"),
    "twin": ("twin", "sibling"),
    "cousin": ("cousin",),
    # Dating: the bare word is the stored predicate; the abbreviations map to it.
    "boyfriend": ("boyfriend",),
    "bf": ("boyfriend",),
    "girlfriend": ("girlfriend",),
    "gf": ("girlfriend",),
    "friend": ("friend", "friend_of", "friendof"),
    "neighbor": ("neighbor",),
    "neighbour": ("neighbor",),
    # Work: the owner reports to their boss; their employer is whom they work for.
    "boss": ("reports_to", "reportsto"),
    "manager": ("reports_to", "reportsto"),
    "supervisor": ("reports_to", "reportsto"),
    "employer": ("employer", "works_for", "worksfor"),
    "report": ("manages",),
    "employee": ("manages",),
    # Care: the owner is treated by their doctor.
    "doctor": ("treated_by", "treatedby", "prescriber"),
    "physician": ("treated_by", "treatedby"),
    "gp": ("treated_by", "treatedby"),
    "dentist": ("treated_by", "treatedby"),
    "prescriber": ("prescriber",),
    # Housing: the owner is a tenant of their landlord.
    "landlord": ("tenant_of", "tenantof"),
    "tenant": ("landlord_of", "landlordof"),
    # Mentorship: the owner is the mentee of their mentor.
    "mentor": ("mentee_of", "menteeof"),
    "mentee": ("mentor_of", "mentorof", "mentors"),
}


def _normalize(word: str) -> str:
    """A relationship word as a lookup key: lowercased, possessive/plural 's
    stripped, trailing punctuation dropped. "Wife's" and "wife" collapse."""
    key = word.strip().lower().strip(".,!?")
    if key.endswith("'s") or key.endswith("’s"):
        key = key[:-2]
    return key.strip()


def predicate_candidates(word: str) -> tuple[str, ...]:
    """The lowercased predicate spellings a relationship word may name, for an
    `lower(predicate) IN (...)` match. Always includes the word itself (so an
    exact predicate name like "owns" or "memberOf" passes through), plus its
    separator-free and underscored forms, then any mapped synonyms. Unknown
    words pass through unchanged — an attractor, never a gate, like
    `normalize_predicate`."""
    key = _normalize(word)
    if not key:
        return ()
    cands: set[str] = {key, _SEP.sub("", key), _SEP.sub("_", key)}
    cands.update(_RELATIONSHIP_WORDS.get(key, ()))
    return tuple(sorted(c for c in cands if c))
