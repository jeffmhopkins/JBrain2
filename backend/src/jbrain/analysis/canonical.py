"""Canonical-name projection and provisional -> confirmed promotion.

`canonical_name` is a denormalized projection of an entity's current name.*
facts (docs/reference/ANALYSIS.md "Entity-row fields", docs/reference/entity.md "Names") — NOT the
frozen first-mention surface form the early pipeline left it as (the bug where a
spouse stayed displayed as the nickname "Sammy"). After a note's facts land, the
touched entities are reprojected by their type's `display_name` precedence. The
owner "Me" keeps its explicit override (the graph's deliberate center).

Promotion confirms a provisional entity once enough distinct notes corroborate
it — the "implicitly confirmed later" ANALYSIS describes. `promote_if_corroborated`
implements it (≥ CORROBORATION_THRESHOLD distinct same-domain notes; a contested
identity routes to a confirm_entity review card instead of auto-confirming). It is
gated by the `entity_promotion` setting (default OFF) and called eager in the
apply path; see docs/reference/entity.md "Entity lifecycle".
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.analysis.entities import alias_owner
from jbrain.schema import get_registry

# value_json keys that may carry a name string (mirrors entities._NAME_VALUE_KEYS).
_NAME_VALUE_KEYS = ("name", "value", "fullname", "alias", "text")


def name_fact_value(predicate: str, value_json: Any) -> str | None:
    """The clean name string a `name.*` fact carries in its value_json, or None.

    Unlike `entities.declared_alias`, this accepts the WHOLE name family
    (including `name.family`/`name.additional`), because the projection composes
    components the alias machinery never registers on their own."""
    base = re.sub(r"[\s_]+", "", predicate).casefold().split(".")[0]
    if base != "name":
        return None
    if isinstance(value_json, str):
        try:
            value_json = json.loads(value_json)
        except (ValueError, TypeError):
            return None
    if not isinstance(value_json, dict):
        return None
    for key in _NAME_VALUE_KEYS:
        val = value_json.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _is_animal_name_fact(predicate: str, value_json: Any) -> bool:
    """A pet's decomposed name fact carries a `species` key
    ({"name": "Ricky", "species": "rat"}). That key is the signal that an entity
    whose (species) `kind` matches no registry type is nonetheless an Animal."""
    if re.sub(r"[\s_]+", "", predicate).casefold().split(".")[0] != "name":
        return False
    if isinstance(value_json, str):
        try:
            value_json = json.loads(value_json)
        except (ValueError, TypeError):
            return False
    return isinstance(value_json, dict) and bool(str(value_json.get("species", "")).strip())


def project_display_name(precedence: tuple[str, ...], values: dict[str, str]) -> str | None:
    """The display name by precedence: the first token that resolves wins. A
    `a+b` token composes components (e.g. name.given + name.family) and only
    fires when EVERY part is present. Tokens with no matching fact (a bare
    "first surface form" hint, an absent slot) are skipped."""
    for token in precedence:
        if "+" in token:
            parts = [values.get(p, "").strip() for p in token.split("+")]
            if all(parts):
                return " ".join(parts)
        elif values.get(token, "").strip():
            return values[token].strip()
    return None


async def reproject_canonical_name(session: AsyncSession, entity_id: uuid.UUID) -> str | None:
    """Recompute and persist an entity's canonical_name from its current name.*
    facts. Returns the new name when it changed, else None (no usable name fact,
    unknown kind, the owner override, or already correct)."""
    row = (
        await session.execute(
            text(
                "SELECT kind, canonical_name, subject_id FROM app.entities"
                " WHERE id = :id AND status != 'merged'"
            ),
            {"id": str(entity_id)},
        )
    ).first()
    if row is None:
        return None
    # The owner "Me" is hard-linked to a subject and pinned to that display name.
    if row.subject_id is not None and row.canonical_name.strip().casefold() == "me":
        return None

    # Active heads only: a name held in pending_review is contested (e.g. an
    # attribute collision), and publishing a contested name as THE display name
    # is the same leak the wiki avoids ("contested facts are withheld",
    # docs/reference/ANALYSIS.md) one layer down.
    facts = (
        await session.execute(
            text(
                "SELECT predicate, value_json FROM app.facts"
                # current value only: a closed (former) name must not re-project
                # the display name (current = active AND valid_to IS NULL).
                # Asserted only (Wave 1, slice 2): the canonical_name is a
                # POSITIVE present identity claim, so a negated name ("not called
                # Bob") or an irrealis/hypothetical name must never become it.
                " WHERE entity_id = :id AND status = 'active' AND valid_to IS NULL"
                "   AND assertion = 'asserted'"
            ),
            {"id": str(entity_id)},
        )
    ).all()
    values: dict[str, str] = {}
    is_animal = False
    for fact in facts:
        name = name_fact_value(fact.predicate, fact.value_json)
        if name is not None:
            values.setdefault(fact.predicate, name)
        is_animal = is_animal or _is_animal_name_fact(fact.predicate, fact.value_json)

    # by_kind is keyed by type id and schema.org name. An animal's kind is its
    # SPECIES ("dog"), which matches no registry type — but its decomposed name
    # fact carries a `species` key, so recognize it and use the Animal type
    # rather than silently skipping every pet.
    registry = get_registry()
    etype = registry.by_kind.get(row.kind)
    if etype is None and is_animal:
        etype = registry.by_kind.get("animal")
    if etype is None:
        return None

    projected = project_display_name(etype.display_name, values)
    if not projected or projected == row.canonical_name:
        return None
    # Don't claim a name a DIFFERENT live entity already owns: that name is
    # contested (a declared-name collision files a merge_proposal that decides
    # identity), and the alias machinery already refused to alias it for the
    # same reason. Reprojecting onto it would pre-empt the human's decision.
    if await alias_owner(session, projected, exclude=entity_id) is not None:
        return None
    await session.execute(
        text("UPDATE app.entities SET canonical_name = :n, updated_at = now() WHERE id = :id"),
        {"n": projected, "id": str(entity_id)},
    )
    return projected


# --- provisional -> confirmed promotion (docs/reference/entity.md "Entity lifecycle")

# Distinct corroborating notes that confirm an entity is real (durable across the
# loss of any one note, and outranking a one-note upstart in a merge — the two
# things `status='confirmed'` already controls in purge.py / entities.plan_merge).
# Conservative N: tuned against the golden harness, not by guess (mirrors
# weight.COMMIT_THRESHOLDS discipline).
CORROBORATION_THRESHOLD = 3


@dataclass(frozen=True)
class PromotionOutcome:
    """What a promotion pass decided for one entity. `action` is `confirmed`
    (auto-promoted in place), `propose` (corroborated but identity contested — the
    caller files a confirm_entity card), or `none` (below threshold / ineligible).
    The name/kind/domain ride along so the caller can build the card without a
    second read."""

    action: str
    entity_id: uuid.UUID
    name: str = ""
    kind: str = ""
    domain: str = "general"


async def corroboration_count(session: AsyncSession, entity_id: uuid.UUID, domain: str) -> int:
    """Distinct SAME-DOMAIN notes that reference the entity via a live, non-derived
    fact (as subject or object) or a mention. Same-domain only: counting across the
    firewall would let a health note's existence be inferred from a general
    entity's status flip. Derived shadows are excluded — a reciprocal inverse edge
    is the same note's claim, not independent corroboration."""
    n = await session.scalar(
        text(
            "SELECT count(DISTINCT note_id) FROM ("
            "  SELECT note_id FROM app.facts"
            "    WHERE (entity_id = :id OR object_entity_id = :id)"
            "      AND derived_from_fact_id IS NULL AND status = 'active'"
            # current corroboration only: a former edge must not inflate the
            # auto-confirm count (current = active AND valid_to IS NULL).
            # Asserted only (Wave 1, slice 2): auto-confirm rests on firm
            # POSITIVE evidence, so a negated or irrealis fact must not count
            # toward it (a mention still corroborates existence regardless).
            "      AND valid_to IS NULL AND assertion = 'asserted' AND domain_code = :dom"
            "  UNION"
            "  SELECT note_id FROM app.entity_mentions"
            "    WHERE entity_id = :id AND domain_code = :dom"
            ") refs"
        ),
        {"id": str(entity_id), "dom": domain},
    )
    return int(n or 0)


async def _has_live_namesake(session: AsyncSession, entity_id: uuid.UUID, domain: str) -> bool:
    """Whether another non-merged same-domain entity shares a normalized alias —
    the "two people, one name" case. Auto-confirming either would cement an
    unresolved identity, so a namesake routes promotion to review instead."""
    return bool(
        await session.scalar(
            text(
                "SELECT 1 FROM app.entity_aliases a"
                " JOIN app.entity_aliases b ON b.alias_norm = a.alias_norm"
                "   AND b.entity_id <> a.entity_id"
                " JOIN app.entities e ON e.id = b.entity_id"
                " WHERE a.entity_id = :id AND e.status <> 'merged'"
                "   AND e.domain_code = :dom LIMIT 1"
            ),
            {"id": str(entity_id), "dom": domain},
        )
    )


async def promote_if_corroborated(session: AsyncSession, entity_id: uuid.UUID) -> PromotionOutcome:
    """Confirm a provisional entity once >= CORROBORATION_THRESHOLD distinct
    same-domain notes corroborate it. Auto-confirms in place (idempotent: a
    guarded UPDATE only ever flips provisional -> confirmed); but when identity is
    contested (a live namesake) it returns `propose` so the caller files a
    confirm_entity card instead of cementing a possibly-wrong identity. The owner
    (subject-linked) and already-confirmed/merged entities are no-ops."""
    row = (
        await session.execute(
            text(
                "SELECT canonical_name, kind, domain_code, status, subject_id FROM app.entities"
                " WHERE id = :id"
            ),
            {"id": str(entity_id)},
        )
    ).first()
    # Only a provisional, non-owner entity is promotable; everything else no-ops.
    if row is None or row.subject_id is not None or row.status != "provisional":
        return PromotionOutcome("none", entity_id)
    if await corroboration_count(session, entity_id, row.domain_code) < CORROBORATION_THRESHOLD:
        return PromotionOutcome("none", entity_id)
    if await _has_live_namesake(session, entity_id, row.domain_code):
        return PromotionOutcome("propose", entity_id, row.canonical_name, row.kind, row.domain_code)
    # status is provisional (read above, same transaction), so the guarded UPDATE
    # always flips it — idempotent across re-analysis (a second run reads the
    # now-confirmed status and no-ops at the guard above).
    await session.execute(
        text(
            "UPDATE app.entities SET status = 'confirmed', updated_at = now()"
            " WHERE id = :id AND status = 'provisional'"
        ),
        {"id": str(entity_id)},
    )
    return PromotionOutcome("confirmed", entity_id, row.canonical_name, row.kind, row.domain_code)
