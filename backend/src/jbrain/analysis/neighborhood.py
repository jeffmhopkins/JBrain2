"""Pure BFS engine for the n-hop entity neighborhood (refocus plan Wave 3).

graph_context's two-layer idiom keeps the DB out of the traversal logic: this
module is pure and fully unit-testable, while the retrieval layer
(`SqlAnalysisRepo.neighborhood`, T3.2) supplies per-hop edge batches from
RLS-scoped queries through the async fetch callback. Recursive CTEs are
deliberately rejected — the shipped `ego_graph` pattern (hops iterated in
Python inside one scoped transaction) is the proven idiom, and per-hop
ranking/caps are awkward in SQL for no benefit at personal-corpus scale.

The engine owns frontier management, first-visit-wins dedup with a parent map
(exactly ONE connecting path per node, e.g.
``Me -spouse-> Celine -co-mention(note X)-> Dr. Patel``), hop stamping, and
the caps. Caps are arguments with owner-ratified defaults (plan §8 decision
11) so evals can tune them without code churn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# Which edge arms a traversal walks. The engine itself is kind-agnostic — the
# retrieval layer honors this by skipping a fetch arm — but the vocabulary
# lives here with the rest of the neighborhood contract shapes.
EdgeKinds = Literal["relationships", "co-mentions", "both"]

MAX_DEPTH = 3
DEFAULT_DEPTH = 2
# 25/hop keeps a 3-hop walk under the total cap while still letting one dense
# hop (a big family/team) come through whole.
DEFAULT_PER_HOP_LIMIT = 25
DEFAULT_TOTAL_CAP = 75
# A note mentioning more than this many distinct entities is a hub (meeting
# minutes, a party): it still surfaces in the notes result but is not expanded
# THROUGH, or one busy note would flood every hop after it.
DEFAULT_HUB_CAP = 8
DEFAULT_NOTE_CAP = 40


@dataclass(frozen=True)
class EntityRef:
    """The node payload an edge carries — all the result needs to render it."""

    id: str
    name: str
    kind: str
    domain: str


@dataclass(frozen=True)
class RefEdge:
    """A typed relationship-fact edge from a frontier entity to a neighbour.

    ``direction`` is relative to the frontier entity ``src_id``: "out" means
    the frontier entity is the fact's subject. ``recency`` (the fact's
    reported_at) orders ref candidates newest-first within a hop.
    """

    src_id: str
    dst: EntityRef
    predicate: str
    direction: Literal["out", "in"] = "out"
    recency: datetime | None = None


@dataclass(frozen=True)
class CoMentionEdge:
    """A shared-note edge: src and dst are mentioned in the same note.

    ``note_entity_count`` is the note's distinct mentioned-entity count — the
    hub-damping input. ``noted_at`` is the note's timestamp, for recency
    ranking.
    """

    src_id: str
    dst: EntityRef
    note_id: str
    note_entity_count: int
    noted_at: datetime | None = None


@dataclass(frozen=True)
class EdgeBatch:
    """Everything leaving one hop's frontier, both edge kinds."""

    refs: tuple[RefEdge, ...] = ()
    co_mentions: tuple[CoMentionEdge, ...] = ()


# (frontier entity ids, hop number 1-based) -> the edges leaving that frontier.
FetchEdges = Callable[[Sequence[str], int], Awaitable[EdgeBatch]]


@dataclass(frozen=True)
class NeighborEntity:
    """One traversed entity, stamped with its hop and its single connecting
    path back to the anchor (the anchor itself is hop 0, path = its name)."""

    id: str
    name: str
    kind: str
    domain: str
    hop: int
    path: str


@dataclass(frozen=True)
class NoteMention:
    """One entity_mentions row for the final entity set (retrieval-supplied)."""

    note_id: str
    entity_id: str
    noted_at: datetime | None = None


@dataclass(frozen=True)
class NeighborhoodNote:
    """A note connecting the neighborhood: min hop over the in-set entities it
    mentions, plus their names (``connects``) so the agent sees WHY it's here."""

    note_id: str
    hop: int
    connects: tuple[str, ...]


@dataclass(frozen=True)
class _Step:
    """Parent-map entry: the edge that first reached a node, pre-rendered."""

    parent_id: str
    label: str


@dataclass(frozen=True)
class _Candidate:
    dst: EntityRef
    step: _Step


def _recency_key(stamp: datetime | None) -> tuple[int, float]:
    """Ascending sort key meaning newest-first, None last."""
    return (1, 0.0) if stamp is None else (0, -stamp.timestamp())


def _ref_label(edge: RefEdge) -> str:
    return f"-{edge.predicate}->" if edge.direction == "out" else f"<-{edge.predicate}-"


def _co_mention_label(edge: CoMentionEdge) -> str:
    return f"-co-mention(note {edge.note_id})->"


def _max_noted(edges: list[CoMentionEdge]) -> datetime | None:
    stamps = [e.noted_at for e in edges if e.noted_at is not None]
    return max(stamps) if stamps else None


def _rank_candidates(
    batch: EdgeBatch, *, hub_cap: int, visited: frozenset[str]
) -> list[_Candidate]:
    """One ranked candidate per unvisited destination.

    Typed-ref candidates rank above co-mention (a curated fact edge beats
    co-occurrence); refs order newest-first, co-mentions by shared-note count
    desc then recency. Co-mention edges through hub notes (more than
    ``hub_cap`` distinct entities) never generate candidates — the note still
    surfaces via mention collection, it just isn't expanded through.
    """
    picked: dict[str, _Candidate] = {}
    # Full tiebreak (through direction/src) so two equally-recent edges to one
    # destination pick the same winner every run — paths must be deterministic.
    for edge in sorted(
        batch.refs,
        key=lambda e: (_recency_key(e.recency), e.dst.id, e.predicate, e.direction, e.src_id),
    ):
        if edge.dst.id in visited or edge.dst.id in picked:
            continue
        picked[edge.dst.id] = _Candidate(edge.dst, _Step(edge.src_id, _ref_label(edge)))
    groups: dict[str, list[CoMentionEdge]] = {}
    for edge in batch.co_mentions:
        if edge.note_entity_count > hub_cap or edge.dst.id in visited or edge.dst.id in picked:
            continue
        groups.setdefault(edge.dst.id, []).append(edge)
    ranked = list(picked.values())
    for edges in sorted(
        groups.values(),
        key=lambda es: (-len({e.note_id for e in es}), _recency_key(_max_noted(es)), es[0].dst.id),
    ):
        # The connecting note shown in the path is the freshest shared one.
        best = min(edges, key=lambda e: (_recency_key(e.noted_at), e.note_id, e.src_id))
        ranked.append(_Candidate(best.dst, _Step(best.src_id, _co_mention_label(best))))
    return ranked


def _render_path(entity_id: str, nodes: dict[str, EntityRef], parents: dict[str, _Step]) -> str:
    parts = [nodes[entity_id].name]
    cursor = entity_id
    while cursor in parents:
        step = parents[cursor]
        parts.append(step.label)
        parts.append(nodes[step.parent_id].name)
        cursor = step.parent_id
    return " ".join(reversed(parts))


async def traverse(
    anchor: EntityRef,
    fetch_edges: FetchEdges,
    *,
    depth: int = DEFAULT_DEPTH,
    per_hop_limit: int = DEFAULT_PER_HOP_LIMIT,
    total_cap: int = DEFAULT_TOTAL_CAP,
    hub_cap: int = DEFAULT_HUB_CAP,
) -> list[NeighborEntity]:
    """BFS out from the anchor over caller-supplied per-hop edge batches.

    Returns entities in hop-then-rank order, the anchor first at hop 0 (it
    counts toward ``total_cap``; degenerate caps clamp so the anchor is always
    returned). ``depth`` clamps to 1..MAX_DEPTH — ego_graph's clamp PATTERN,
    with the plan-mandated 1..3 bound (§5 T3.2; ego_graph itself caps at 2).
    First visit wins: the best-ranked edge that reaches a node becomes its
    parent, so every entity carries exactly one connecting path — a node seen
    at hop 1 is never restamped by a hop-2 edge.
    """
    hops = max(1, min(depth, MAX_DEPTH))
    total_cap = max(1, total_cap)
    per_hop_limit = max(0, per_hop_limit)
    nodes: dict[str, EntityRef] = {anchor.id: anchor}
    hop_of: dict[str, int] = {anchor.id: 0}
    parents: dict[str, _Step] = {}
    order: list[str] = [anchor.id]
    frontier: list[str] = [anchor.id]
    for hop in range(1, hops + 1):
        if not frontier or len(order) >= total_cap:
            break
        batch = await fetch_edges(tuple(frontier), hop)
        admitted: list[str] = []
        for cand in _rank_candidates(batch, hub_cap=hub_cap, visited=frozenset(nodes)):
            if len(admitted) >= per_hop_limit or len(order) >= total_cap:
                break
            # Contract guard: an edge whose src is outside the traversed set
            # would install an unrenderable parent and KeyError at build time —
            # fail loudly at the seam where the retrieval bug actually is.
            if cand.step.parent_id not in nodes:
                raise ValueError(
                    f"edge batch references src_id outside the traversed set: {cand.step.parent_id}"
                )
            nodes[cand.dst.id] = cand.dst
            hop_of[cand.dst.id] = hop
            parents[cand.dst.id] = cand.step
            order.append(cand.dst.id)
            admitted.append(cand.dst.id)
        frontier = admitted
    return [
        NeighborEntity(
            id=eid,
            name=nodes[eid].name,
            kind=nodes[eid].kind,
            domain=nodes[eid].domain,
            hop=hop_of[eid],
            path=_render_path(eid, nodes, parents),
        )
        for eid in order
    ]


def assemble_notes(
    entities: Sequence[NeighborEntity],
    mentions: Iterable[NoteMention],
    *,
    note_cap: int = DEFAULT_NOTE_CAP,
) -> list[NeighborhoodNote]:
    """Fold mention rows for the traversed entity set into the notes result.

    Notes are collected from mentions only (§8 decision 11): each note stamps
    ``min(hop)`` over the in-set entities it mentions and carries their names
    (hop-then-name order) as ``connects``. Note order is hop, then recency
    (newest first), then id; capped at ``note_cap``. Mentions of entities
    outside the set are ignored, so the retrieval layer may over-fetch. Hub
    notes reappear here naturally — damping only stops expansion THROUGH them.
    """
    by_id = {e.id: e for e in entities}
    hops: dict[str, int] = {}
    stamps: dict[str, datetime | None] = {}
    connected: dict[str, dict[str, NeighborEntity]] = {}
    for mention in mentions:
        entity = by_id.get(mention.entity_id)
        if entity is None:
            continue
        hops[mention.note_id] = min(hops.get(mention.note_id, entity.hop), entity.hop)
        current = stamps.get(mention.note_id)
        if mention.noted_at is not None and (current is None or mention.noted_at > current):
            stamps[mention.note_id] = mention.noted_at
        else:
            stamps.setdefault(mention.note_id, None)
        connected.setdefault(mention.note_id, {})[entity.id] = entity
    ordered = sorted(hops, key=lambda nid: (hops[nid], _recency_key(stamps[nid]), nid))
    return [
        NeighborhoodNote(
            note_id=nid,
            hop=hops[nid],
            connects=tuple(
                e.name for e in sorted(connected[nid].values(), key=lambda e: (e.hop, e.name))
            ),
        )
        for nid in ordered[:note_cap]
    ]
