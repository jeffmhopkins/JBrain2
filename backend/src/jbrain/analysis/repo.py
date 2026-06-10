"""Read and review-resolution queries for the analysis API.

Shapes returned here ARE the wire contract (api/analysis.py serializes them
verbatim); the frontend is built against them. Everything runs on RLS-scoped
sessions, so pre-P7 "owner-only" is enforced by Postgres, not checked here.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

SNIPPET_CHARS = 240

REVIEW_STATUSES = ("open", "resolved", "dismissed")


class UnknownAction(Exception):
    """The resolve action is not valid for the item's kind."""


class AlreadyResolved(Exception):
    """The review item is no longer open."""


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


_FACT_SELECT = f"""
    SELECT f.id::text, f.entity_id::text, e.canonical_name AS entity_name,
           f.predicate, f.qualifier, f.kind, f.statement, f.value_json,
           f.assertion, f.status, f.pinned, f.confidence,
           f.valid_from, f.valid_to, f.reported_at, f.temporal_precision,
           left(c.text, {SNIPPET_CHARS}) AS source_snippet
    FROM app.facts f
    JOIN app.entities e ON e.id = f.entity_id
    LEFT JOIN app.chunks c ON c.id = f.chunk_id
"""


def _fact_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "entity_id": row.entity_id,
        "entity_name": row.entity_name,
        "predicate": row.predicate,
        "qualifier": row.qualifier,
        "kind": row.kind,
        "statement": row.statement,
        "value_json": row.value_json,
        "assertion": row.assertion,
        "status": row.status,
        "pinned": row.pinned,
        "confidence": row.confidence,
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
        "reported_at": row.reported_at,
        "temporal_precision": row.temporal_precision,
        "source_snippet": row.source_snippet,
    }


class SqlAnalysisRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def note_analysis_view(self, ctx: SessionContext, note_id: str) -> dict[str, Any] | None:
        """The GET /notes/{id}/analysis payload; None when the note is
        unknown (or invisible — RLS makes the two indistinguishable)."""
        nid = _as_uuid(note_id)
        if nid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            note = (
                await session.execute(
                    text("SELECT id FROM app.notes WHERE id = :id AND deleted_at IS NULL"),
                    {"id": str(nid)},
                )
            ).first()
            if note is None:
                return None
            header = (
                await session.execute(
                    text(
                        "SELECT title, tags, analyzed_at, extractor"
                        " FROM app.note_analysis WHERE note_id = :id"
                    ),
                    {"id": str(nid)},
                )
            ).first()
            facts = (
                await session.execute(
                    text(_FACT_SELECT + " WHERE f.note_id = :id ORDER BY f.created_at, f.id"),
                    {"id": str(nid)},
                )
            ).all()
            entities = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT e.id::text, e.kind, e.canonical_name, e.status
                        FROM app.entities e
                        WHERE e.id IN (
                            SELECT entity_id FROM app.facts WHERE note_id = :id
                            UNION
                            SELECT object_entity_id FROM app.facts
                            WHERE note_id = :id AND object_entity_id IS NOT NULL
                            UNION
                            SELECT entity_id FROM app.entity_mentions WHERE note_id = :id
                        )
                        ORDER BY e.canonical_name
                        """
                    ),
                    {"id": str(nid)},
                )
            ).all()
            tokens = (
                await session.execute(
                    text(
                        "SELECT id::text, surface_phrase, kind, resolved_start, resolved_end,"
                        " temporal_precision FROM app.temporal_tokens"
                        " WHERE note_id = :id ORDER BY created_at, id"
                    ),
                    {"id": str(nid)},
                )
            ).all()
        return {
            "note_id": str(nid),
            "title": header.title if header else None,
            "tags": list(header.tags) if header else [],
            "analyzed_at": header.analyzed_at if header else None,
            "extractor": header.extractor if header else None,
            "facts": [_fact_dict(f) for f in facts],
            "entities": [
                {"id": e.id, "kind": e.kind, "name": e.canonical_name, "status": e.status}
                for e in entities
            ],
            "temporal_tokens": [
                {
                    "id": t.id,
                    "surface_phrase": t.surface_phrase,
                    "kind": t.kind,
                    "resolved_start": t.resolved_start,
                    "resolved_end": t.resolved_end,
                    "temporal_precision": t.temporal_precision,
                }
                for t in tokens
            ],
        }

    async def entity_view(self, ctx: SessionContext, entity_id: str) -> dict[str, Any] | None:
        eid = _as_uuid(entity_id)
        if eid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            entity = (
                await session.execute(
                    text(
                        "SELECT id::text, kind, canonical_name, status, domain_code"
                        " FROM app.entities WHERE id = :id"
                    ),
                    {"id": str(eid)},
                )
            ).first()
            if entity is None:
                return None
            aliases = (
                await session.execute(
                    text(
                        "SELECT alias FROM app.entity_aliases WHERE entity_id = :id ORDER BY alias"
                    ),
                    {"id": str(eid)},
                )
            ).scalars()
            facts = (
                await session.execute(
                    text(
                        _FACT_SELECT + " WHERE f.entity_id = :id"
                        " ORDER BY f.predicate, f.qualifier,"
                        # History newest-first by validity, capture-time
                        # tie-break — mirrors the supersession ordering.
                        " coalesce(f.valid_from, f.reported_at) DESC, f.reported_at DESC,"
                        " f.created_at DESC"
                    ),
                    {"id": str(eid)},
                )
            ).all()
            inbound = (
                await session.execute(
                    text(
                        """
                        SELECT f.entity_id::text, e.canonical_name AS name,
                               f.predicate, f.statement
                        FROM app.facts f JOIN app.entities e ON e.id = f.entity_id
                        WHERE f.object_entity_id = :id AND f.status = 'active'
                        ORDER BY f.created_at DESC
                        """
                    ),
                    {"id": str(eid)},
                )
            ).all()
            mentions = (
                await session.execute(
                    text(
                        f"""
                        SELECT m.note_id::text,
                               coalesce(left(c.text, {SNIPPET_CHARS}), m.surface_text) AS snippet,
                               m.created_at
                        FROM app.entity_mentions m
                        LEFT JOIN app.chunks c ON c.id = m.chunk_id
                        WHERE m.entity_id = :id
                        ORDER BY m.created_at DESC
                        """
                    ),
                    {"id": str(eid)},
                )
            ).all()

        predicates: dict[tuple[str, str], dict[str, Any]] = {}
        for row in facts:
            key = (row.predicate, row.qualifier)
            group = predicates.setdefault(
                key,
                {
                    "predicate": row.predicate,
                    "qualifier": row.qualifier,
                    "current": None,
                    "history": [],
                },
            )
            shaped = _fact_dict(row)
            group["history"].append(shaped)
            if group["current"] is None and row.status == "active":
                group["current"] = shaped

        return {
            "id": entity.id,
            "kind": entity.kind,
            "canonical_name": entity.canonical_name,
            "status": entity.status,
            "aliases": list(aliases),
            "domain": entity.domain_code,
            "predicates": list(predicates.values()),
            "inbound": [
                {
                    "entity_id": r.entity_id,
                    "name": r.name,
                    "predicate": r.predicate,
                    "statement": r.statement,
                }
                for r in inbound
            ],
            "mentions": [
                {"note_id": m.note_id, "snippet": m.snippet, "created_at": m.created_at}
                for m in mentions
            ],
        }

    async def list_review(self, ctx: SessionContext, status: str) -> list[dict[str, Any]]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id::text, kind, payload, domain_code, created_at"
                        " FROM app.review_items WHERE status = :status"
                        " ORDER BY created_at, id"
                    ),
                    {"status": status},
                )
            ).all()
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "payload": r.payload,
                "domain": r.domain_code,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    async def resolve_review(
        self, ctx: SessionContext, item_id: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply a resolution; returns the updated item, None when unknown.

        Raises:
            UnknownAction: the action is invalid for the item's kind.
            AlreadyResolved: the item is not open.
        """
        iid = _as_uuid(item_id)
        if iid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            item = (
                await session.execute(
                    text(
                        "SELECT id::text, kind, payload, status, domain_code, created_at"
                        " FROM app.review_items WHERE id = :id FOR UPDATE"
                    ),
                    {"id": str(iid)},
                )
            ).first()
            if item is None:
                return None
            if item.status != "open":
                raise AlreadyResolved(item.status)

            new_status = await self._apply_resolution(
                session, item.kind, item.payload, action, payload
            )

            await session.execute(
                text(
                    "UPDATE app.review_items"
                    " SET status = :status, resolution = cast(:resolution AS jsonb),"
                    "     resolved_at = now()"
                    " WHERE id = :id"
                ),
                {
                    "id": str(iid),
                    "status": new_status,
                    "resolution": _json({"action": action, "payload": payload}),
                },
            )
            updated = (
                await session.execute(
                    text(
                        "SELECT id::text, kind, payload, status, resolution, domain_code,"
                        " created_at, resolved_at FROM app.review_items WHERE id = :id"
                    ),
                    {"id": str(iid)},
                )
            ).one()
        return {
            "id": updated.id,
            "kind": updated.kind,
            "payload": updated.payload,
            "status": updated.status,
            "resolution": updated.resolution,
            "domain": updated.domain_code,
            "created_at": updated.created_at,
            "resolved_at": updated.resolved_at,
        }

    async def _apply_resolution(
        self,
        session: AsyncSession,
        kind: str,
        item_payload: dict[str, Any],
        action: str,
        payload: dict[str, Any],
    ) -> str:
        """Per-kind resolution semantics; returns the item's new status."""
        if action == "dismiss":
            return "dismissed"

        if kind in ("attribute_collision", "fact_conflict") and action in ("accept_a", "accept_b"):
            winner = item_payload.get("fact_a" if action == "accept_a" else "fact_b")
            loser = item_payload.get("fact_b" if action == "accept_a" else "fact_a")
            if not winner or not loser:
                raise UnknownAction(f"item payload lacks fact_a/fact_b for {action!r}")
            # Pinning is what makes the human decision survive reprocessing.
            await session.execute(
                text(
                    "UPDATE app.facts SET status = 'active', pinned = true,"
                    " superseded_by = NULL WHERE id = :id"
                ),
                {"id": winner},
            )
            await session.execute(
                text("UPDATE app.facts SET status = 'retracted' WHERE id = :id"),
                {"id": loser},
            )
            return "resolved"

        if kind == "merge_proposal" and action in ("accept", "reject"):
            entity_a, entity_b = item_payload.get("entity_a"), item_payload.get("entity_b")
            if not entity_a or not entity_b:
                raise UnknownAction("item payload lacks entity_a/entity_b")
            if action == "accept":
                # Tombstone + repoint; span-anchored mentions keep this
                # reversible (un-merge = re-resolve the spans).
                await session.execute(
                    text(
                        "UPDATE app.entities SET status = 'merged', merged_into_id = :keep,"
                        " updated_at = now() WHERE id = :gone"
                    ),
                    {"keep": entity_a, "gone": entity_b},
                )
                for stmt in (
                    "UPDATE app.entity_mentions SET entity_id = :keep WHERE entity_id = :gone",
                    "UPDATE app.facts SET entity_id = :keep WHERE entity_id = :gone",
                    "UPDATE app.facts SET object_entity_id = :keep WHERE object_entity_id = :gone",
                ):
                    await session.execute(text(stmt), {"keep": entity_a, "gone": entity_b})
            else:
                # Permanent negative knowledge: never re-proposed.
                a, b = sorted((entity_a, entity_b))
                await session.execute(
                    text(
                        "INSERT INTO app.entity_distinctions"
                        " (id, entity_a, entity_b, reason, domain_code)"
                        " SELECT gen_random_uuid(), :a, :b, 'merge rejected', domain_code"
                        " FROM app.entities WHERE id = :a"
                        " ON CONFLICT (entity_a, entity_b) DO NOTHING"
                    ),
                    {"a": a, "b": b},
                )
            return "resolved"

        if kind == "domain_promotion" and action in ("accept", "reject"):
            if action == "accept":
                fact_id = item_payload.get("fact_id")
                proposed = item_payload.get("proposed_domain")
                if not fact_id or not proposed:
                    raise UnknownAction("item payload lacks fact_id/proposed_domain")
                await session.execute(
                    text(
                        "UPDATE app.facts SET domain_code = :domain, pinned = true WHERE id = :id"
                    ),
                    {"id": fact_id, "domain": proposed},
                )
            return "resolved"

        raise UnknownAction(f"action {action!r} is not valid for kind {kind!r}")


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, default=_json_default)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)
