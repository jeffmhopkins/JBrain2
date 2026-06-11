"""Canonical-name projection and provisional -> confirmed promotion.

`canonical_name` is a denormalized projection of an entity's current name.*
facts (docs/ANALYSIS.md "Entity-row fields", docs/entity.md "Names") — NOT the
frozen first-mention surface form the early pipeline left it as (the bug where a
spouse stayed displayed as the nickname "Sammy"). After a note's facts land, the
touched entities are reprojected by their type's `display_name` precedence. The
owner "Me" keeps its explicit override (the graph's deliberate center).

Promotion confirms a provisional entity once a SECOND note corroborates it —
the "implicitly confirmed later" ANALYSIS describes but the code never did
(nothing but the hard-coded "Me" was ever confirmed).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    # by_kind is keyed by type id and schema.org name. KNOWN LIMITATION: an
    # animal entity's kind is its species ("dog"), which matches no registry
    # type, so animals do not reproject — acceptable today because a pet's
    # canonical is already its name, but a real gap if a pet first mentioned by
    # a reference ("the rat") later declares a name. Unifying kind resolution
    # (the resolver's species/noun matching) is the proper fix.
    etype = get_registry().by_kind.get(row.kind)
    if etype is None:
        return None

    # Active heads only: a name held in pending_review is contested (e.g. an
    # attribute collision), and publishing a contested name as THE display name
    # is the same leak the wiki avoids ("contested facts are withheld",
    # docs/ANALYSIS.md) one layer down.
    facts = (
        await session.execute(
            text(
                "SELECT predicate, value_json FROM app.facts"
                " WHERE entity_id = :id AND status = 'active'"
            ),
            {"id": str(entity_id)},
        )
    ).all()
    values: dict[str, str] = {}
    for fact in facts:
        name = name_fact_value(fact.predicate, fact.value_json)
        if name is not None:
            values.setdefault(fact.predicate, name)

    projected = project_display_name(etype.display_name, values)
    if not projected or projected == row.canonical_name:
        return None
    await session.execute(
        text("UPDATE app.entities SET canonical_name = :n, updated_at = now() WHERE id = :id"),
        {"n": projected, "id": str(entity_id)},
    )
    return projected
