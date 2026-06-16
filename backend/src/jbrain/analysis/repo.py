"""Read and review-resolution queries for the analysis API.

Shapes returned here ARE the wire contract (api/analysis.py serializes them
verbatim); the frontend is built against them. Everything runs on RLS-scoped
sessions, so pre-P7 "owner-only" is enforced by Postgres, not checked here.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.consolidation import rewrite_predicate
from jbrain.analysis.display import mark_snippet
from jbrain.analysis.entities import are_distinct, merge_entity_pair, plan_merge
from jbrain.analysis.predicates import decide_predicate, raw_descriptor
from jbrain.analysis.supersession import is_functional
from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import EmbedClient
from jbrain.schema import get_registry
from jbrain.workflow import events as wf_events

# The list view exposes three lanes: "open" is the pending triage queue,
# "deferred" is the parked lane (defer + discuss), and "resolved" folds in
# dismissals and reopened tombstones — there is no separate dismissed listing.
REVIEW_STATUSES = ("open", "resolved", "deferred")

# Parking actions: they move an item to the deferred lane and write no graph
# effects, so reopening one is a bare re-queue. "discuss" additionally marks
# the row as handed to the assistant (a follow-up wires the actual handoff).
DEFER_ACTIONS = ("defer", "discuss")


def _consolidate_enqueued(effects: list[dict[str, Any]]) -> bool:
    """Whether this resolution warrants a consolidate sweep — true exactly when the
    effects carry a `predicate_remapped` action (the map_to_existing / suggest_better
    paths that heal stored facts onto a canonical). The signal the resolution.changed
    event keys on: since the W2·C cutover the event dispatcher (not a direct enqueue)
    submits the consolidate job, so the emitted event is the SOLE trigger for the
    sweep — this predicate gates whether the event is even emitted."""
    return any(e.get("action") == "predicate_remapped" for e in effects)


async def _emit_resolution_event(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    domain_code: str,
    item_id: str,
    effects: list[dict[str, Any]],
) -> None:
    """Emit a resolution.changed event that DRIVES the consolidate sweep (W2·C live):
    the event dispatcher resolves it to the consolidate pipeline and enqueues the job.
    Emitted only when this resolution remapped a predicate (`_consolidate_enqueued`),
    so the event and the sweep stay 1:1. domain is the review item's fail-closed E2
    stamp; the principal is the resolving owner. The `_shadow_enqueued` baseline is
    retained on the event for the dispatcher's observability diff (it logs would-vs-
    baseline even live), not as a second enqueue.

    Emitted AFTER the resolution transaction commits, in its OWN best-effort session
    (wf_events.emit_event swallows every error): an event whose insert fails — e.g. a
    test whose owner principal is not a real row — must never abort the resolution,
    which is why this is a separate transaction, not the resolution's. The cost is
    that the event is not atomic with the resolution: a crash between commit and emit
    drops the sweep trigger — but the boot `backfill_consolidate` self-heal and the
    idempotent sweep are the safety net, so a missed event self-heals on the next
    sweep rather than stranding the remap."""
    if not _consolidate_enqueued(effects):
        return
    if not ctx.principal_id:
        return
    await wf_events.emit_event(
        maker,
        ctx,
        type=wf_events.RESOLUTION_CHANGED,
        domain_code=domain_code,
        payload={"item_id": item_id},
        enqueued=wf_events.shadow_enqueued("consolidate_predicates", {}),
        principal_id=ctx.principal_id,
    )


class UnknownAction(Exception):
    """The resolve action is not valid for the item's kind."""


class AlreadyResolved(Exception):
    """The review item is no longer open."""


class AlreadyOpen(Exception):
    """The reopen target is already in the open queue."""


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class MergeOutcome:
    """The result of enacting an owner-approved merge proposal: which id survived,
    which was folded, and whether this call did the fold (a re-enact of an already
    merged pair is a no-op)."""

    keep_id: str
    gone_id: str
    merged: bool


_FACT_SELECT = """
    SELECT f.id::text, f.entity_id::text, e.canonical_name AS entity_name,
           f.predicate, f.qualifier, f.kind, f.statement, f.value_json,
           f.assertion, f.status, f.pinned, f.confidence,
           f.valid_from, f.valid_to, f.reported_at, f.temporal_precision,
           f.object_entity_id::text AS object_entity_id,
           oe.canonical_name AS object_entity_name,
           oe.domain_code AS object_entity_domain,
           c.text AS chunk_text, anchor.char_start, anchor.char_end
    FROM app.facts f
    JOIN app.entities e ON e.id = f.entity_id
    LEFT JOIN app.entities oe ON oe.id = f.object_entity_id
    LEFT JOIN app.chunks c ON c.id = f.chunk_id
    LEFT JOIN LATERAL (
        SELECT m.char_start, m.char_end
        FROM app.entity_mentions m
        WHERE m.chunk_id = f.chunk_id AND m.entity_id = f.entity_id
        ORDER BY (m.char_end - m.char_start) DESC, m.char_start
        LIMIT 1
    ) anchor ON true
"""
# The lateral picks the subject's mention in the fact's cited chunk — the
# span the UI highlights. Widest-first ordering keeps zero-width paraphrase
# anchors from shadowing a real span; with none in range the snippet is
# served unmarked.
# The `oe` join resolves a relationship fact's object to a node so the UI can
# render `me.owns → F-150` as an entity link, not bury it in the statement
# sentence. The join is RLS-scoped like every other read here: an object the
# session can't see yields a null name, and the frontend falls back to the
# statement rather than offer a chip that would 404 — never a cross-firewall
# name leak.


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
        "object_entity_id": row.object_entity_id,
        "object_entity_name": row.object_entity_name,
        "object_entity_domain": row.object_entity_domain,
        "source_snippet": mark_snippet(row.chunk_text, row.char_start, row.char_end),
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

    async def list_entities(
        self,
        ctx: SessionContext,
        q: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """The GET /entities browse list: non-merged entities with the glance
        counts the rows render. fact_count is the entity's live edges
        (active + pending review); last_seen is the newest reported_at across
        all its subject facts — null for an entity known only by mention,
        which sorts last."""
        where = ["e.status <> 'merged'"]
        params: dict[str, Any] = {"limit": limit}
        if kind is not None:
            where.append("e.kind = :kind")
            params["kind"] = kind
        for i, token in enumerate(q.split() if q else []):
            # Each whitespace token must hit the name or SOME alias — different
            # tokens may land on different aliases, so "Jeff Hopkins" matches an
            # entity aliased "Jeff" and "Jeffrey Mark Hopkins" even though no one
            # alias holds that contiguous run. AND across tokens keeps precision
            # (a stray "Smith" still excludes). Each token is a literal substring,
            # never a pattern — escape the LIKE wildcards so "100%" stays literal.
            escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params[f"tok{i}"] = f"%{escaped}%"
            where.append(
                f"(e.canonical_name ILIKE :tok{i} ESCAPE '\\' OR EXISTS ("
                f" SELECT 1 FROM app.entity_aliases a"
                f" WHERE a.entity_id = e.id AND a.alias ILIKE :tok{i} ESCAPE '\\'))"
            )
        sql = f"""
            SELECT e.id::text, e.kind, e.canonical_name, e.status, e.domain_code,
                   (SELECT coalesce(array_agg(a.alias ORDER BY a.alias), '{{}}')
                    FROM app.entity_aliases a WHERE a.entity_id = e.id) AS aliases,
                   (SELECT count(*) FROM app.facts f
                    WHERE f.entity_id = e.id
                      AND f.status IN ('active', 'pending_review')) AS fact_count,
                   (SELECT count(*) FROM app.entity_mentions m
                    WHERE m.entity_id = e.id) AS mention_count,
                   (SELECT max(f.reported_at) FROM app.facts f
                    WHERE f.entity_id = e.id) AS last_seen
            FROM app.entities e
            WHERE {" AND ".join(where)}
            ORDER BY last_seen DESC NULLS LAST, e.canonical_name
            LIMIT :limit
        """
        async with scoped_session(self._maker, ctx) as session:
            entities = (await session.execute(text(sql), params)).all()
        return [
            {
                "id": e.id,
                "kind": e.kind,
                "canonical_name": e.canonical_name,
                "status": e.status,
                "domain": e.domain_code,
                "aliases": list(e.aliases),
                "fact_count": e.fact_count,
                "mention_count": e.mention_count,
                "last_seen": e.last_seen,
            }
            for e in entities
        ]

    async def relate(
        self,
        ctx: SessionContext,
        anchor_id: str | None,
        predicates: Sequence[str],
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Follow an anchor entity's outbound relationship edges, returning the
        entities they point at. `anchor_id=None` anchors on the owner's "Me"
        entity — the implicit center of the graph — so "my wife" needs no id.
        `predicates` is the candidate predicate spellings (from
        `relationships.predicate_candidates`); a fact matches when its lowercased
        predicate is among them. Active, asserted ref edges only. The object join
        is RLS-scoped, so a relation whose target sits behind a firewall this
        session can't see simply doesn't come back — never a cross-domain leak.
        Rows mirror `list_entities` (id/kind/canonical_name/domain/aliases) plus
        the matched `predicate`, so the agent tools reuse one render path."""
        preds = sorted({p.lower() for p in predicates if p})
        if not preds:
            return []
        async with scoped_session(self._maker, ctx) as session:
            if anchor_id is None:
                anchor = (
                    await session.execute(
                        text(
                            "SELECT id::text FROM app.entities"
                            " WHERE subject_id IS NOT NULL AND lower(canonical_name) = 'me'"
                            " AND status <> 'merged' LIMIT 1"
                        )
                    )
                ).scalar()
                if anchor is None:
                    return []
            else:
                aid = _as_uuid(anchor_id)
                if aid is None:
                    return []
                anchor = str(aid)
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT oe.id::text, oe.kind, oe.canonical_name,
                               oe.domain_code, f.predicate,
                               (SELECT coalesce(array_agg(a.alias ORDER BY a.alias), '{}')
                                FROM app.entity_aliases a WHERE a.entity_id = oe.id) AS aliases
                        FROM app.facts f
                        JOIN app.entities oe ON oe.id = f.object_entity_id
                        WHERE f.entity_id = :anchor
                          AND lower(f.predicate) IN :preds
                          AND f.status = 'active' AND f.assertion = 'asserted'
                          AND oe.status <> 'merged'
                        ORDER BY f.reported_at DESC NULLS LAST, oe.canonical_name
                        LIMIT :limit
                        """
                    ).bindparams(bindparam("preds", expanding=True)),
                    {"anchor": anchor, "preds": preds, "limit": limit},
                )
            ).all()
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "canonical_name": r.canonical_name,
                "domain": r.domain_code,
                "aliases": list(r.aliases),
                "predicate": r.predicate,
            }
            for r in rows
        ]

    async def ego_graph(
        self, ctx: SessionContext, entity_id: str, depth: int = 1
    ) -> dict[str, Any] | None:
        """The graph view's ego subgraph: the focal entity plus everything
        within `depth` relationship hops in either direction, as nodes plus
        directed edges. Active asserted relationship facts only; merged
        tombstones are skipped. Every query is RLS-scoped, so a neighbour (or
        the edge to it) behind a firewall this session can't see never comes
        back — never a cross-domain leak. Node rows mirror `list_entities`
        (id/kind/canonical_name/status/domain) so the UI reuses one render path.
        """
        eid = _as_uuid(entity_id)
        if eid is None:
            return None
        hops = max(1, min(depth, 2))
        out_sql = text(
            "SELECT f.entity_id::text AS src, f.object_entity_id::text AS dst, f.predicate"
            " FROM app.facts f JOIN app.entities oe ON oe.id = f.object_entity_id"
            " WHERE f.entity_id IN :ids AND f.object_entity_id IS NOT NULL"
            " AND f.status = 'active' AND f.assertion = 'asserted' AND oe.status <> 'merged'"
        ).bindparams(bindparam("ids", expanding=True))
        in_sql = text(
            "SELECT f.entity_id::text AS src, f.object_entity_id::text AS dst, f.predicate"
            " FROM app.facts f JOIN app.entities se ON se.id = f.entity_id"
            " WHERE f.object_entity_id IN :ids"
            " AND f.status = 'active' AND f.assertion = 'asserted' AND se.status <> 'merged'"
        ).bindparams(bindparam("ids", expanding=True))
        async with scoped_session(self._maker, ctx) as session:
            root = (
                await session.execute(
                    text(
                        "SELECT id::text, kind, canonical_name, status, domain_code"
                        " FROM app.entities WHERE id = :id AND status <> 'merged'"
                    ),
                    {"id": str(eid)},
                )
            ).first()
            if root is None:
                return None
            node_ids: set[str] = {root.id}
            frontier: set[str] = {root.id}
            edges: dict[tuple[str, str, str], dict[str, str]] = {}
            for _ in range(hops):
                if not frontier:
                    break
                ids = sorted(frontier)
                rows = [
                    *(await session.execute(out_sql, {"ids": ids})).all(),
                    *(await session.execute(in_sql, {"ids": ids})).all(),
                ]
                nxt: set[str] = set()
                for r in rows:
                    edges.setdefault(
                        (r.src, r.dst, r.predicate),
                        {"source": r.src, "target": r.dst, "predicate": r.predicate},
                    )
                    for nid in (r.src, r.dst):
                        if nid not in node_ids:
                            node_ids.add(nid)
                            nxt.add(nid)
                frontier = nxt
            nodes = (
                await session.execute(
                    text(
                        "SELECT id::text, kind, canonical_name, status, domain_code"
                        " FROM app.entities WHERE id IN :ids"
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"ids": sorted(node_ids)},
                )
            ).all()
        return {
            "root": root.id,
            "depth": hops,
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "canonical_name": n.canonical_name,
                    "status": n.status,
                    "domain": n.domain_code,
                }
                for n in nodes
            ],
            "edges": list(edges.values()),
        }

    async def full_graph(self, ctx: SessionContext) -> dict[str, Any]:
        """The graph view's default: every non-merged entity as a node — even
        ones with no relationships — plus all active asserted relationship edges
        between visible entities. RLS-scoped, so firewalled entities and any
        edge touching one never come back. `root` is the owner's "Me" entity
        (the natural center the UI pins), or "" when there is none. Node rows
        mirror `ego_graph`/`list_entities` so the UI reuses one render path."""
        async with scoped_session(self._maker, ctx) as session:
            nodes = (
                await session.execute(
                    text(
                        "SELECT id::text, kind, canonical_name, status, domain_code"
                        " FROM app.entities WHERE status <> 'merged'"
                    )
                )
            ).all()
            # The object join (and RLS) drops edges to an entity this session
            # can't see; subject side is filtered to the visible node set below.
            edge_rows = (
                await session.execute(
                    text(
                        "SELECT f.entity_id::text AS src, f.object_entity_id::text AS dst,"
                        " f.predicate FROM app.facts f"
                        " JOIN app.entities se ON se.id = f.entity_id"
                        " JOIN app.entities oe ON oe.id = f.object_entity_id"
                        " WHERE f.object_entity_id IS NOT NULL"
                        " AND f.status = 'active' AND f.assertion = 'asserted'"
                        " AND se.status <> 'merged' AND oe.status <> 'merged'"
                    )
                )
            ).all()
            me = (
                await session.execute(
                    text(
                        "SELECT id::text FROM app.entities"
                        " WHERE subject_id IS NOT NULL AND lower(canonical_name) = 'me'"
                        " AND status <> 'merged' LIMIT 1"
                    )
                )
            ).scalar()
        node_ids = {n.id for n in nodes}
        edges: dict[tuple[str, str, str], dict[str, str]] = {}
        for r in edge_rows:
            if r.src in node_ids and r.dst in node_ids:
                edges.setdefault(
                    (r.src, r.dst, r.predicate),
                    {"source": r.src, "target": r.dst, "predicate": r.predicate},
                )
        return {
            "root": me or "",
            "depth": 0,
            "nodes": [
                {
                    "id": n.id,
                    "kind": n.kind,
                    "canonical_name": n.canonical_name,
                    "status": n.status,
                    "domain": n.domain_code,
                }
                for n in nodes
            ],
            "edges": list(edges.values()),
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
                        # "Linked from" is OTHER entities' edges into this one, so
                        # the auto-materialized reciprocal of one of THIS entity's
                        # own outbound edges (Me.children -> kid mirrored as
                        # kid.parent -> Me) is dropped: it is the same bond, already
                        # shown as the subject's own predicate, not an independent
                        # inbound claim. Either side of the pair may be the derived
                        # one, so match the reciprocal link in both directions.
                        """
                        SELECT f.entity_id::text, e.canonical_name AS name,
                               f.predicate, f.statement
                        FROM app.facts f JOIN app.entities e ON e.id = f.entity_id
                        WHERE f.object_entity_id = :id AND f.status = 'active'
                          AND NOT EXISTS (
                              SELECT 1 FROM app.facts mine
                              WHERE mine.entity_id = :id
                                AND (mine.id = f.derived_from_fact_id
                                     OR mine.derived_from_fact_id = f.id)
                          )
                        ORDER BY f.created_at DESC
                        """
                    ),
                    {"id": str(eid)},
                )
            ).all()
            mentions = (
                await session.execute(
                    text(
                        """
                        SELECT m.note_id::text, m.surface_text,
                               m.char_start, m.char_end,
                               c.text AS chunk_text, m.created_at
                        FROM app.entity_mentions m
                        LEFT JOIN app.chunks c ON c.id = m.chunk_id
                        WHERE m.entity_id = :id
                        ORDER BY m.created_at DESC
                        """
                    ),
                    {"id": str(eid)},
                )
            ).all()

        # A non-functional relationship is set-valued: every distinct object is a
        # co-equal LIVE value (the pipeline keeps them all active —
        # supersession.decide accumulates them), not a revision of one slot. So
        # those group per object — each child its own current edge — while scalar
        # attributes and functional relationships (one current value, prior values
        # are superseded history) keep their single (predicate, qualifier) slot.
        # Without the object split, four active children collapse to one "current"
        # plus a misleading "3 earlier", disagreeing with the map's four edges.
        predicates: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        for row in facts:
            set_valued = row.kind == "relationship" and not is_functional(row.predicate)
            key = (row.predicate, row.qualifier, row.object_entity_id if set_valued else None)
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
            # "Current" means active AND OPEN (valid_to IS NULL); a closed
            # interval is a FORMER value, history not the live head, even when
            # nothing replaced it (docs/research/legacy-links-handling.md §3.1).
            if group["current"] is None and row.status == "active" and row.valid_to is None:
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
                {
                    "note_id": m.note_id,
                    # A re-chunked note can orphan the span: fall back to the
                    # bare surface text rather than dropping the mention.
                    "snippet": mark_snippet(m.chunk_text, m.char_start, m.char_end)
                    or m.surface_text,
                    "created_at": m.created_at,
                }
                for m in mentions
            ],
        }

    async def merge_entities(
        self, ctx: SessionContext, entity_a: str, entity_b: str
    ) -> MergeOutcome:
        """Enact a merge the owner approved (the agent's merge_entities leaf). The
        agent never picks the survivor: plan_merge re-ranks the pair so the
        more-anchored identity is kept (the owner is never merged away), a permanent
        distinct_from blocks the fold, and a re-enact whose pair already merged is a
        no-op (idempotent). Runs through the same merge_entity_pair the review inbox
        uses, in the caller's RLS scope."""
        a, b = _as_uuid(entity_a), _as_uuid(entity_b)
        if a is None or b is None or a == b:
            raise UnknownAction("merge_entities needs two distinct entity ids")
        async with scoped_session(self._maker, ctx) as session:
            rows = {
                str(r.id): r
                for r in (
                    await session.execute(
                        text("SELECT id, status FROM app.entities WHERE id = ANY(:ids)"),
                        {"ids": [str(a), str(b)]},
                    )
                ).all()
            }
            if str(a) not in rows or str(b) not in rows:
                raise UnknownAction("merge_entities: an entity id is not in scope")
            # A re-enacted proposal whose pair was already merged is a no-op — never
            # a second fold onto a tombstone.
            if any(r.status == "merged" for r in rows.values()):
                return MergeOutcome(str(a), str(b), merged=False)
            if await are_distinct(session, a, b):
                raise UnknownAction("a permanent distinct_from forbids merging these")
            plan = await plan_merge(session, a, b)
            await merge_entity_pair(session, keep=plan.keep_id, gone=plan.gone_id)
            return MergeOutcome(str(plan.keep_id), str(plan.gone_id), merged=True)

    async def note_currency(
        self, ctx: SessionContext, note_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Per note id, the facts derived from it that are NO LONGER the live
        value: `superseded` (a newer note replaced it), `retracted` (no longer
        asserted — an extraction error or a correction), or `pending_review`
        (contested, unverified). Each carries the entity it belongs to and, for a
        superseded value, the CURRENT active value on that same
        entity.predicate[.qualifier] address.

        Runs in the caller's RLS scope, so out-of-scope facts never leak. This is
        what lets the agent's retrieval tools overlay the supersession/review
        outcome onto raw note prose: the note text is the original record, but the
        graph knows what has since changed. Derived inverse shadows are excluded —
        they mirror a primary edge, so reporting them would double-count the
        reciprocal.
        """
        ids = [str(u) for u in (_as_uuid(n) for n in note_ids) if u is not None]
        if not ids:
            return {}
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT f.note_id::text AS note_id, e.id::text AS entity_id,
                               e.canonical_name AS entity_name, f.predicate,
                               f.qualifier, f.status, f.statement AS stale_value,
                               cur.statement AS current_value
                        FROM app.facts f
                        JOIN app.entities e ON e.id = f.entity_id
                        LEFT JOIN LATERAL (
                            SELECT a.statement FROM app.facts a
                            WHERE a.entity_id = f.entity_id
                              AND a.predicate = f.predicate
                              AND a.qualifier = f.qualifier
                              AND a.status = 'active' AND a.valid_to IS NULL
                            ORDER BY coalesce(a.valid_from, a.reported_at) DESC,
                                     a.reported_at DESC, a.created_at DESC
                            LIMIT 1
                        ) cur ON true
                        WHERE f.note_id::text = ANY(:ids)
                          AND f.derived_from_fact_id IS NULL
                          AND f.status IN ('superseded', 'retracted', 'pending_review')
                        ORDER BY f.note_id, f.predicate, f.qualifier,
                                 coalesce(f.valid_from, f.reported_at) DESC
                        """
                    ),
                    {"ids": ids},
                )
            ).all()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r.note_id, []).append(
                {
                    "entity_id": r.entity_id,
                    "entity_name": r.entity_name,
                    "predicate": r.predicate,
                    "qualifier": r.qualifier,
                    "status": r.status,
                    "stale_value": r.stale_value,
                    "current_value": r.current_value,
                }
            )
        return out

    async def list_review(self, ctx: SessionContext, status: str) -> list[dict[str, Any]]:
        """Open queue oldest-first for triage; the resolved log (decisions,
        dismissals, and reopened tombstones) newest-decision-first.

        A reopened item appears in BOTH segments: live in the open queue and
        struck-through in the log, ordered by its reopened_at marker."""
        if status == "open":
            where = "status = 'open'"
            order = "created_at, id"
        elif status == "deferred":
            where = "status = 'deferred'"
            order = "resolved_at DESC, created_at, id"
        else:
            where = (
                "status IN ('resolved', 'dismissed')"
                " OR (status = 'open' AND resolution ? 'reopened_at')"
            )
            order = "coalesce(resolved_at, (resolution->>'reopened_at')::timestamptz) DESC, id"
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id::text, kind, payload, status, resolution, domain_code,"
                        f" created_at, resolved_at FROM app.review_items WHERE {where}"
                        f" ORDER BY {order}"
                    )
                )
            ).all()
        return [_item_dict(r) for r in rows]

    async def predicate_suggestions(
        self, ctx: SessionContext, item_id: str, *, embedder: EmbedClient, k: int = 5
    ) -> list[dict[str, Any]] | None:
        """The canonicals nearest a review item's proposed predicate, weighted by
        cosine similarity (strongest first) — the ranked options the
        correct-in-place predicate picker offers. Computed on demand so every
        open card gets live suggestions, including ones filed before the picker
        existed. None when the item doesn't exist; [] when it carries no
        predicate (e.g. an identity card)."""
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text("SELECT payload FROM app.review_items WHERE id = :id"),
                    {"id": item_id},
                )
            ).first()
            if row is None:
                return None
            payload = row.payload
            predicate = payload.get("predicate")
            if not isinstance(predicate, str) or not predicate:
                return []
            statement = payload.get("statement")
            kind = payload.get("fact_kind")
            decision = await decide_predicate(
                session,
                predicate=predicate,
                statement=statement if isinstance(statement, str) else "",
                kind=kind if isinstance(kind, str) else None,
                embedder=embedder,
                k=k,
            )
        return [{"name": n, "score": s} for n, s in decision.suggestions]

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

            new_status, effects = await self._apply_resolution(
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
                    "resolution": _json({"action": action, "payload": payload, "effects": effects}),
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
            item_domain = item.domain_code
        # Emit the resolution.changed event AFTER the resolution committed, in its own
        # best-effort session, so a failed event can never abort the resolution; the
        # dispatcher enqueues the consolidate sweep off it (W2·C live).
        await _emit_resolution_event(
            self._maker, ctx, domain_code=item_domain, item_id=str(iid), effects=effects
        )
        return _item_dict(updated)

    async def resolve_review_batch(
        self, ctx: SessionContext, decisions: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Apply many resolutions in one RLS-scoped transaction; returns
        {"items": [updated], "errors": [{id, detail}]}.

        Each decision carries its own action — the caller (which knows each
        row's kind) picks the right verb per item, so a bulk "approve all" is
        a list of the correct per-kind actions, not one action guessed here.
        A bad item is collected as an error and skipped; the good ones still
        commit, mirroring the per-item optimistic UI.
        """
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        # (item_id, domain_code, effects) for each committed resolution that
        # remapped a predicate — emitted as resolution.changed events AFTER the batch
        # transaction commits, never inside it (a failed emit must not roll back the
        # batch); the dispatcher enqueues the consolidate sweep off each (W2·C live).
        to_emit: list[tuple[str, str, list[dict[str, Any]]]] = []
        async with scoped_session(self._maker, ctx) as session:
            for decision in decisions:
                item_id = str(decision.get("id", ""))
                action = str(decision.get("action", ""))
                payload = decision.get("payload") or {}
                iid = _as_uuid(item_id)
                if iid is None or not action:
                    errors.append({"id": item_id, "detail": "bad id or action"})
                    continue
                row = (
                    await session.execute(
                        text(
                            "SELECT kind, payload, status, domain_code FROM app.review_items"
                            " WHERE id = :id FOR UPDATE"
                        ),
                        {"id": str(iid)},
                    )
                ).first()
                if row is None:
                    errors.append({"id": item_id, "detail": "not found"})
                    continue
                if row.status != "open":
                    errors.append({"id": item_id, "detail": "not open"})
                    continue
                try:
                    new_status, effects = await self._apply_resolution(
                        session, row.kind, row.payload, action, payload
                    )
                except UnknownAction as exc:
                    errors.append({"id": item_id, "detail": str(exc)})
                    continue
                to_emit.append((str(iid), row.domain_code, effects))
                await session.execute(
                    text(
                        "UPDATE app.review_items"
                        " SET status = :status, resolution = cast(:resolution AS jsonb),"
                        "     resolved_at = now() WHERE id = :id"
                    ),
                    {
                        "id": str(iid),
                        "status": new_status,
                        "resolution": _json(
                            {"action": action, "payload": payload, "effects": effects}
                        ),
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
                items.append(_item_dict(updated))
        # Emits after the batch committed, each in its own best-effort session — a
        # failed event can never roll back the resolutions; the dispatcher enqueues
        # each consolidate sweep off the event (W2·C live).
        for emit_item_id, emit_domain, emit_effects in to_emit:
            await _emit_resolution_event(
                self._maker,
                ctx,
                domain_code=emit_domain,
                item_id=emit_item_id,
                effects=emit_effects,
            )
        return {"items": items, "errors": errors}

    async def reopen_review(self, ctx: SessionContext, item_id: str) -> dict[str, Any] | None:
        """Full unwind: reverse the recorded resolution effects in the same
        transaction that re-queues the item; returns the updated item (with a
        reopen_note for effects that are permanent by doctrine), None when
        unknown.

        Raises:
            AlreadyOpen: the item is not resolved/dismissed.
        """
        iid = _as_uuid(item_id)
        if iid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            item = (
                await session.execute(
                    text(
                        "SELECT id::text, status, resolution"
                        " FROM app.review_items WHERE id = :id FOR UPDATE"
                    ),
                    {"id": str(iid)},
                )
            ).first()
            if item is None:
                return None
            if item.status == "open":
                raise AlreadyOpen(item.status)

            resolution = dict(item.resolution or {})
            # Un-parking a deferred item is a clean return to pending: it was
            # never decided, wrote no effects, so it leaves no reopened
            # tombstone — the resolution is cleared outright.
            if item.status == "deferred":
                notes: list[str] = []
                await session.execute(
                    text(
                        "UPDATE app.review_items SET status = 'open', resolved_at = NULL,"
                        " resolution = NULL WHERE id = :id"
                    ),
                    {"id": str(iid)},
                )
            else:
                notes = await self._reverse_effects(session, resolution.get("effects") or [])
                # The marker is how the UI tombstones the log row; the next
                # resolve overwrites the whole resolution and clears it.
                resolution["reopened_at"] = datetime.now(UTC).isoformat()
                await session.execute(
                    text(
                        "UPDATE app.review_items SET status = 'open', resolved_at = NULL,"
                        " resolution = cast(:resolution AS jsonb) WHERE id = :id"
                    ),
                    {"id": str(iid), "resolution": _json(resolution)},
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
        out = _item_dict(updated)
        out["reopen_note"] = "; ".join(notes) if notes else None
        return out

    async def _apply_resolution(
        self,
        session: AsyncSession,
        kind: str,
        item_payload: dict[str, Any],
        action: str,
        payload: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Per-kind resolution semantics; returns (new_status, effects).

        Effects capture the prior state each write destroyed — exactly
        enough for reopen_review to reverse it. Recording must never change
        what a resolution does, only remember it.
        """
        if action == "dismiss":
            # No graph writes: reopening a dismissal is a bare re-queue.
            return "dismissed", []

        if action in DEFER_ACTIONS:
            # Parking, not deciding: the item leaves the open queue with no
            # graph effects, so reopen is a bare re-queue. The recorded action
            # ("defer" vs "discuss") is how the deferred lane tags each row.
            return "deferred", []

        if action == "correct":
            # Correction-note path (docs/DESIGN.md "Edit model"): the human's
            # fix was filed as a real note (the #7 channel) — its id rides in
            # the payload. The graph change is the pipeline's when it processes
            # that note, so the resolve writes no facts; it only closes the
            # item and remembers the link. Reopen keeps the note (it stands on
            # its own), so there is nothing to unwind.
            note_id = payload.get("note_id")
            if not note_id:
                raise UnknownAction("correct requires a note_id")
            return "resolved", [{"action": "corrected", "note_id": note_id}]

        if kind in ("attribute_collision", "fact_conflict") and action in ("accept_a", "accept_b"):
            winner = item_payload.get("fact_a" if action == "accept_a" else "fact_b")
            loser = item_payload.get("fact_b" if action == "accept_a" else "fact_a")
            if not winner or not loser:
                raise UnknownAction(f"item payload lacks fact_a/fact_b for {action!r}")
            prior = {
                row.id: row
                for row in (
                    await session.execute(
                        text(
                            "SELECT id::text AS id, status, pinned,"
                            " superseded_by::text AS superseded_by"
                            " FROM app.facts WHERE id IN (:winner, :loser)"
                        ),
                        {"winner": winner, "loser": loser},
                    )
                ).all()
            }
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
            effects: list[dict[str, Any]] = []
            if winner in prior:
                w = prior[winner]
                effects.append(
                    {
                        "action": "pinned",
                        "fact_id": winner,
                        "prior_status": w.status,
                        "prior_pinned": w.pinned,
                        "prior_superseded_by": w.superseded_by,
                    }
                )
            if loser in prior:
                effects.append(
                    {
                        "action": "retracted",
                        "fact_id": loser,
                        "prior_status": prior[loser].status,
                    }
                )
            # Cascade onto derived shadows so a reciprocal can't outlive the
            # human's verdict (red-team Finding 3): the winner's shadow goes
            # active alongside it, the loser's shadow retracts with it. Recorded
            # with the "pinned" shape so reopen restores each shadow's prior
            # status/superseded_by (shadows are never themselves pinned).
            for source_id, becomes_active in ((winner, True), (loser, False)):
                shadows = (
                    await session.execute(
                        text(
                            "SELECT id::text AS id, status, pinned,"
                            " superseded_by::text AS superseded_by"
                            " FROM app.facts WHERE derived_from_fact_id = :sid"
                        ),
                        {"sid": source_id},
                    )
                ).all()
                for sh in shadows:
                    if becomes_active:
                        await session.execute(
                            text(
                                "UPDATE app.facts SET status = 'active',"
                                " superseded_by = NULL WHERE id = :id"
                            ),
                            {"id": sh.id},
                        )
                    else:
                        await session.execute(
                            text("UPDATE app.facts SET status = 'retracted' WHERE id = :id"),
                            {"id": sh.id},
                        )
                    effects.append(
                        {
                            "action": "pinned",
                            "fact_id": sh.id,
                            "prior_status": sh.status,
                            "prior_pinned": sh.pinned,
                            "prior_superseded_by": sh.superseded_by,
                        }
                    )
            return "resolved", effects

        if kind == "merge_proposal" and action in ("accept", "reject"):
            entity_a, entity_b = item_payload.get("entity_a"), item_payload.get("entity_b")
            if not entity_a or not entity_b:
                raise UnknownAction("item payload lacks entity_a/entity_b")
            if action == "accept":
                # Tombstone + repoint via the shared fold (merge_entity_pair).
                # RETURNING captures the repointed row ids — un-merge moves exactly
                # those rows back instead of guessing from spans.
                gone_prior = (
                    await session.execute(
                        text(
                            "SELECT status, merged_into_id::text AS merged_into"
                            " FROM app.entities WHERE id = :gone"
                        ),
                        {"gone": entity_b},
                    )
                ).first()
                repointed = await merge_entity_pair(session, keep=entity_a, gone=entity_b)
                return "resolved", [
                    {
                        "action": "merged",
                        "entity_id": entity_b,
                        "into": entity_a,
                        "prior_status": gone_prior.status if gone_prior else None,
                        "prior_merged_into": gone_prior.merged_into if gone_prior else None,
                        **repointed,
                    }
                ]
            # Permanent negative knowledge: never re-proposed.
            a, b = sorted((entity_a, entity_b))
            inserted = (
                await session.execute(
                    text(
                        "INSERT INTO app.entity_distinctions"
                        " (id, entity_a, entity_b, reason, domain_code)"
                        " SELECT gen_random_uuid(), :a, :b, 'merge rejected', domain_code"
                        " FROM app.entities WHERE id = :a"
                        " ON CONFLICT (entity_a, entity_b) DO NOTHING"
                        " RETURNING id::text"
                    ),
                    {"a": a, "b": b},
                )
            ).first()
            return "resolved", [
                {"action": "distinct_from", "a": a, "b": b, "inserted": inserted is not None}
            ]

        if kind in ("ambiguous_mention", "extraction_truncated", "new_predicate") and (
            action == "reject"
        ):
            # An informational card's only advertised verb: it wrote no graph
            # state, so resolving it is a dismissal. ambiguous_mention may be
            # re-proposed with more signal; extraction_truncated is acknowledged
            # (the owner re-runs with a larger budget if they want the tail);
            # a new_predicate card dismissal leaves the fact under its raw name.
            return "dismissed", []

        if kind == "new_predicate" and action in (
            "accept_as_new",
            "suggest_better",
            "map_to_existing",
        ):
            raw = item_payload.get("predicate")
            if not raw:
                raise UnknownAction("new_predicate card payload lacks 'predicate'")
            if action == "map_to_existing":
                canonical = payload.get("canonical_name")
                if not canonical:
                    raise UnknownAction("map_to_existing requires a canonical_name")
                known = (
                    await session.execute(
                        text("SELECT 1 FROM app.canonical_predicates WHERE canonical_name = :c"),
                        {"c": canonical},
                    )
                ).first()
                if known is None:
                    raise UnknownAction(f"map target {canonical!r} is not a canonical predicate")
                # Heal stored facts raw -> canonical directly (the shared, guarded
                # rewrite the nightly sweep uses): there is no runtime renamed_from
                # store the YAML-backed normalize_predicate reads, so the durable
                # registry alias is a Phase-5 correction note.
                rewritten = await rewrite_predicate(session, raw, canonical)
                # Forward-compat: once the alias lands in the registry (Phase 5),
                # the sweep heals any row this pass had to skip. The sweep is now
                # enqueued by the event dispatcher (W2·C cutover): the
                # `predicate_remapped` effect below drives `_emit_resolution_event`,
                # whose resolution.changed event the engine resolves to the
                # consolidate pipeline — the direct enqueue here is gone.
                return "resolved", [
                    {
                        "action": "predicate_remapped",
                        "raw": raw,
                        "canonical": canonical,
                        "fact_ids": rewritten,
                    }
                ]

            # accept_as_new / suggest_better -> mint the predicate into the index.
            # The minted row's embedding is left NULL; the sync_predicates job
            # backfills it (and never clobbers a minted row). A suggested name is
            # trimmed so a stray-whitespace variant can't mint a near-duplicate
            # canonical (the UI trims too, but the invariant is enforced here).
            name = payload.get("canonical_name") if action == "suggest_better" else raw
            if action == "suggest_better" and isinstance(name, str):
                name = name.strip()
            if not name:
                raise UnknownAction("suggest_better requires a canonical_name")
            fact_kind = item_payload.get("fact_kind")
            if not fact_kind:
                raise UnknownAction("new_predicate card payload lacks 'fact_kind'")
            # A name the registry already declares is its own canonical — minting a
            # 'minted' row would shadow the seed sync (origin='seed') will install,
            # so skip the mint and let the registry own it.
            if not get_registry().declares_predicate(name):
                descriptor = raw_descriptor(name, item_payload.get("statement", ""), fact_kind)
                # A relationship predicate's value is an edge; everything else a
                # scalar for now (typed-shape inference is Phase-5 work).
                value_shape = "ref" if fact_kind == "relationship" else "scalar"
                await session.execute(
                    text(
                        "INSERT INTO app.canonical_predicates"
                        " (canonical_name, descriptor, value_shape, kind, functional, origin)"
                        " VALUES (:name, :descriptor, :shape, :kind, false, 'minted')"
                        " ON CONFLICT (canonical_name) DO NOTHING"
                    ),
                    {
                        "name": name,
                        "descriptor": descriptor,
                        "shape": value_shape,
                        "kind": fact_kind,
                    },
                )
            effects: list[dict[str, Any]] = [{"action": "minted", "canonical_name": name}]
            # suggest_better corrects the predicate's NAME, so the already-committed
            # fact adopts it: heal raw -> name in place (the same guarded rewrite
            # map_to_existing uses). The consolidate sweep is enqueued by the event
            # dispatcher now (W2·C): the `predicate_remapped` effect drives the
            # resolution.changed event, not a direct enqueue. accept_as_new keeps the
            # raw name (name == raw), so there is nothing to move.
            if name != raw:
                rewritten = await rewrite_predicate(session, raw, name)
                effects.insert(
                    0,
                    {
                        "action": "predicate_remapped",
                        "raw": raw,
                        "canonical": name,
                        "fact_ids": rewritten,
                    },
                )
            return "resolved", effects

        if kind == "domain_promotion" and action in ("accept", "reject"):
            if action == "accept":
                fact_id = item_payload.get("fact_id")
                proposed = item_payload.get("proposed_domain")
                if not fact_id or not proposed:
                    raise UnknownAction("item payload lacks fact_id/proposed_domain")
                prior = (
                    await session.execute(
                        text("SELECT domain_code, pinned FROM app.facts WHERE id = :id"),
                        {"id": fact_id},
                    )
                ).first()
                await session.execute(
                    text(
                        "UPDATE app.facts SET domain_code = :domain, pinned = true WHERE id = :id"
                    ),
                    {"id": fact_id, "domain": proposed},
                )
                if prior is None:
                    return "resolved", []
                return "resolved", [
                    {
                        "action": "domain_changed",
                        "fact_id": fact_id,
                        "prior_domain": prior.domain_code,
                        "prior_pinned": prior.pinned,
                        "new_domain": proposed,
                    }
                ]
            return "resolved", []

        if kind == "low_confidence_inference" and action in ("accept", "reject"):
            # The held fact lives as a pending_review row linked by fact_id.
            # accept pins it active (pinning survives the re-analysis sweep, like
            # domain_promotion / collision accept); reject retracts it. Both use
            # the pinned/retracted effect shapes _reverse_effects already undoes,
            # so reopen restores the pending_review state with no new code. A
            # missing fact_id is a malformed card; a vanished row (swept) just
            # closes the card.
            fact_id = item_payload.get("fact_id")
            if not fact_id:
                raise UnknownAction("low_confidence_inference resolution requires a fact_id")
            prior = (
                await session.execute(
                    text(
                        "SELECT status, pinned, superseded_by::text AS superseded_by"
                        " FROM app.facts WHERE id = :id"
                    ),
                    {"id": fact_id},
                )
            ).first()
            if prior is None:
                return "resolved", []
            if action == "accept":
                await session.execute(
                    text(
                        "UPDATE app.facts SET status = 'active', pinned = true,"
                        " superseded_by = NULL WHERE id = :id"
                    ),
                    {"id": fact_id},
                )
                return "resolved", [
                    {
                        "action": "pinned",
                        "fact_id": fact_id,
                        "prior_status": prior.status,
                        "prior_pinned": prior.pinned,
                        "prior_superseded_by": prior.superseded_by,
                    }
                ]
            await session.execute(
                text("UPDATE app.facts SET status = 'retracted' WHERE id = :id"),
                {"id": fact_id},
            )
            return "resolved", [
                {"action": "retracted", "fact_id": fact_id, "prior_status": prior.status}
            ]

        if kind == "confirm_entity" and action in ("accept", "reject"):
            # A corroborated-but-contested entity (a live namesake). accept
            # confirms it (the guarded UPDATE only flips provisional -> confirmed,
            # so a vanished/merged/already-confirmed entity just closes the card);
            # reject leaves it provisional. The entity_confirmed effect lets reopen
            # restore the prior status.
            entity_id = item_payload.get("entity_id")
            if not entity_id:
                raise UnknownAction("confirm_entity resolution requires an entity_id")
            prior = (
                await session.execute(
                    text("SELECT status FROM app.entities WHERE id = :id"),
                    {"id": entity_id},
                )
            ).first()
            if prior is None or action == "reject":
                return "resolved", []
            await session.execute(
                text(
                    "UPDATE app.entities SET status = 'confirmed', updated_at = now()"
                    " WHERE id = :id AND status = 'provisional'"
                ),
                {"id": entity_id},
            )
            return "resolved", [
                {"action": "entity_confirmed", "entity_id": entity_id, "prior_status": prior.status}
            ]

        raise UnknownAction(f"action {action!r} is not valid for kind {kind!r}")

    async def _reverse_effects(
        self, session: AsyncSession, effects: list[dict[str, Any]]
    ) -> list[str]:
        """Undo recorded effects newest-first; returns notes for the ones
        that are permanent by doctrine and deliberately survive."""
        notes: list[str] = []
        for effect in reversed(effects):
            action = effect.get("action")
            if action == "pinned":
                await session.execute(
                    text(
                        "UPDATE app.facts SET status = :status, pinned = :pinned,"
                        " superseded_by = :superseded_by WHERE id = :id"
                    ),
                    {
                        "id": effect["fact_id"],
                        "status": effect["prior_status"],
                        "pinned": effect["prior_pinned"],
                        "superseded_by": effect["prior_superseded_by"],
                    },
                )
            elif action == "retracted":
                await session.execute(
                    text("UPDATE app.facts SET status = :status WHERE id = :id"),
                    {"id": effect["fact_id"], "status": effect["prior_status"]},
                )
            elif action == "merged":
                await session.execute(
                    text(
                        "UPDATE app.entities SET status = :status, merged_into_id = :merged_into,"
                        " updated_at = now() WHERE id = :id"
                    ),
                    {
                        "id": effect["entity_id"],
                        "status": effect["prior_status"],
                        "merged_into": effect["prior_merged_into"],
                    },
                )
                # Repoint only the rows the merge moved — rows linked to the
                # survivor before the merge stay put.
                for key, stmt in (
                    ("mention_ids", "UPDATE app.entity_mentions SET entity_id = :gone"),
                    ("fact_ids", "UPDATE app.facts SET entity_id = :gone"),
                    ("object_fact_ids", "UPDATE app.facts SET object_entity_id = :gone"),
                ):
                    for row_id in effect.get(key) or []:
                        await session.execute(
                            text(f"{stmt} WHERE id = :id"),
                            {"gone": effect["entity_id"], "id": row_id},
                        )
            elif action == "entity_confirmed":
                await session.execute(
                    text(
                        "UPDATE app.entities SET status = :status, updated_at = now()"
                        " WHERE id = :id"
                    ),
                    {"id": effect["entity_id"], "status": effect["prior_status"]},
                )
            elif action == "domain_changed":
                await session.execute(
                    text(
                        "UPDATE app.facts SET domain_code = :domain, pinned = :pinned"
                        " WHERE id = :id"
                    ),
                    {
                        "id": effect["fact_id"],
                        "domain": effect["prior_domain"],
                        "pinned": effect["prior_pinned"],
                    },
                )
            elif action == "distinct_from":
                # docs/ANALYSIS.md: distinct_from is permanent — the item
                # re-queues but the edge stays.
                notes.append(
                    "the distinct-from edge is permanent and stays — this pair is never re-proposed"
                )
            elif action == "corrected":
                # The correction note is the human's own note: reopening the
                # review item re-queues it but never deletes the note.
                notes.append("the correction note stays — it was filed as your own note")
            elif action == "predicate_remapped":
                # Move the rewritten facts back to their raw predicate — but only
                # rows STILL at the canonical from this map (a later supersession
                # or re-map may have moved one on; don't clobber that).
                for row_id in effect.get("fact_ids") or []:
                    await session.execute(
                        text(
                            "UPDATE app.facts SET predicate = :raw"
                            " WHERE id = :id AND predicate = :canon"
                        ),
                        {"raw": effect["raw"], "id": row_id, "canon": effect["canonical"]},
                    )
            elif action == "minted":
                # A minted canonical predicate is durable vocabulary other facts
                # may already use; reopening re-queues the card but never un-mints
                # it (deleting would orphan adopters). The Phase-5 loop prunes.
                notes.append("the minted predicate stays — it is now part of the vocabulary")
        return notes


def _item_dict(row: Any) -> dict[str, Any]:
    """One review-item wire shape everywhere: list, resolve, and reopen."""
    return {
        "id": row.id,
        "kind": row.kind,
        "payload": row.payload,
        "status": row.status,
        "resolution": row.resolution,
        "domain": row.domain_code,
        "created_at": row.created_at,
        "resolved_at": row.resolved_at,
    }


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, default=_json_default)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)
