"""The owner-only deterministic Person⇄device binding.

`entities.subject_id` is the join between a graph Device entity and its operational
`subjects(kind='device')` row — the human↔track link. Today it is set in exactly
one place (the "Me" hard-link in `analysis/entities.py`); every Device entity is
born `subject_id=None` and OwnTracks subjects are minted on a disjoint path with no
back-reference. This reconciler is the ONLY other writer of `subject_id`, and it is
deliberately mechanical:

  * it links a Device entity ONLY when that entity carries an `operatedBy`→Person
    fact (so the binding is owner-asserted via a note, never inferred) AND its name
    or an external identifier EXACTLY matches a `subjects(kind='device')` row;
  * it is NEVER LLM-chosen and NEVER fuzzy — a display-name match is spoofable, so a
    near match is no match. A non-exact or ambiguous result leaves `subject_id=None`;
  * it runs under the full-owner pipeline/sweep session (the SYSTEM_CTX the geofence
    projection already runs in), mirroring the "Me" hard-link mechanism.

Fail-closed: an unlinked Device yields zero fixes through it (Person→entity→
`subject_id`→fixes never finds a subject), which is the privacy-safe default.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The relationship predicate that marks a Device entity as a person's device. Set
# in device.yaml; matched here as free text (the predicate the fact carries).
_OPERATED_BY = "operatedBy"
# The ExternalIdentified predicate whose value is a strong external key.
_IDENTIFIER = "identifier"


async def reconcile_device_bindings(session: AsyncSession, entity_ids: set[uuid.UUID]) -> int:
    """Bind each touched Device entity to its `subjects(kind='device')` row when an
    `operatedBy` fact and an EXACT name/identifier match both hold; return the count
    bound. Non-device / already-bound / unmatched entities are no-ops. Runs inside
    the caller's full-owner transaction, so it is atomic with the graph write."""
    if not entity_ids:
        return 0
    candidates = (
        await session.execute(
            text(
                "SELECT e.id::text AS eid, e.canonical_name"
                " FROM app.entities e"
                " WHERE e.id = ANY(:ids) AND lower(e.kind) = 'device'"
                "   AND e.subject_id IS NULL AND e.status != 'merged'"
                "   AND EXISTS ("
                "     SELECT 1 FROM app.facts f"
                "     WHERE f.entity_id = e.id AND f.predicate = :op"
                "       AND f.kind = 'relationship' AND f.status = 'active'"
                "       AND f.assertion = 'asserted' AND f.object_entity_id IS NOT NULL"
                "   )"
            ),
            {"ids": [str(eid) for eid in entity_ids], "op": _OPERATED_BY},
        )
    ).all()

    bound = 0
    for cand in candidates:
        subject_id = await _match_subject(session, cand.eid, cand.canonical_name)
        if subject_id is None:
            continue  # no EXACT subject: leave unlinked (fail-closed)
        # Bind only when the chosen subject is not already claimed by another entity:
        # the join is one-to-one, and silently stealing a bound subject is the wrong
        # link no automated step may make.
        updated = (
            await session.execute(
                text(
                    "UPDATE app.entities SET subject_id = cast(:sid AS uuid), updated_at = now()"
                    " WHERE id = cast(:eid AS uuid) AND subject_id IS NULL"
                    "   AND NOT EXISTS ("
                    "     SELECT 1 FROM app.entities o WHERE o.subject_id = cast(:sid AS uuid)"
                    "   )"
                    " RETURNING id"
                ),
                {"sid": subject_id, "eid": cand.eid},
            )
        ).first()
        if updated is not None:
            bound += 1
    return bound


async def sweep_device_bindings(session: AsyncSession) -> int:
    """Bind every still-unlinked Device entity that now has an `operatedBy` fact and
    an exact subject match — the self-healing backstop for a dropped fact-apply hook,
    run from the geofence sweep's full-owner session. Returns the count bound."""
    ids = {
        row[0]
        for row in (
            await session.execute(
                text(
                    "SELECT e.id FROM app.entities e"
                    " WHERE lower(e.kind) = 'device' AND e.subject_id IS NULL"
                    "   AND e.status != 'merged'"
                )
            )
        ).all()
    }
    return await reconcile_device_bindings(session, ids)


async def _match_subject(
    session: AsyncSession, entity_id: str, canonical_name: str | None
) -> str | None:
    """The single `subjects(kind='device')` row this Device entity EXACTLY names, or
    None when zero or more than one match. The match key is the device's name (its
    canonical name or any alias) against `subjects.display_name`, OR an external
    `identifier` fact value. Exact only — diacritic/whitespace tolerance would open
    the spoofable fuzzy match the design refuses."""
    rows = (
        await session.execute(
            text(
                "SELECT DISTINCT s.id::text AS sid FROM app.subjects s"
                " WHERE s.kind = 'device' AND ("
                "   s.display_name = :name"
                "   OR s.display_name IN ("
                "     SELECT a.alias FROM app.entity_aliases a WHERE a.entity_id = :eid"
                "   )"
                "   OR s.display_name IN ("
                "     SELECT COALESCE(f.value_json->>'value', f.value_json#>>'{}')"
                "     FROM app.facts f"
                "     WHERE f.entity_id = :eid AND f.predicate = :ident"
                "       AND f.status = 'active'"
                "       AND COALESCE(f.value_json->>'value', f.value_json#>>'{}') IS NOT NULL"
                "   )"
                " )"
            ),
            {"name": canonical_name, "eid": entity_id, "ident": _IDENTIFIER},
        )
    ).all()
    return rows[0].sid if len(rows) == 1 else None
