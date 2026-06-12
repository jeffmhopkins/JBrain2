"""The Proposal engine: staging, dependency-safe partial approval, and enactment
(docs/ASSISTANT.md "Staging & approval").

The tree logic is pure and the load-bearing safety property: the owner approves
the whole tree, a subtree, or a single leaf, and the executor enacts a leaf **only
when every prerequisite it depends on is also approved**. An approved leaf whose
prerequisite was rejected is **held, never enacted** — so no partial selection can
leave the knowledge base inconsistent (fail-closed). The agent's authority never
changes: each enacted leaf is one bounded, owner-authorised operation run by the
trusted executor; rationale text in a node is data, never instruction (#1).
"""

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

# A dependency is "satisfied" when the node it points to is approved or already
# enacted; anything else (pending, rejected, held) leaves it unmet.
_SATISFIED = frozenset(("approved", "enacted"))


@dataclass(frozen=True)
class Node:
    """The pure shape the tree logic reasons over (a row's safety-relevant fields)."""

    id: str
    parent_id: str | None
    type: str  # group | leaf
    status: str  # pending | approved | rejected | enacted | held
    deps: tuple[str, ...] = ()


def _children(nodes: Sequence[Node], node_id: str) -> list[Node]:
    return [n for n in nodes if n.parent_id == node_id]


def descendants(nodes: Sequence[Node], node_id: str) -> list[str]:
    """Every node beneath `node_id` (depth-first), excluding itself."""
    out: list[str] = []
    stack = [c.id for c in _children(nodes, node_id)]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(c.id for c in _children(nodes, current))
    return out


def cascade_approve(nodes: Sequence[Node], node_id: str) -> dict[str, str]:
    """Approve a node and its subtree by containment, returning each id's new
    status. An individually-rejected descendant is left rejected — selection
    cascades, but an explicit override wins."""
    by_id = {n.id: n for n in nodes}
    changes: dict[str, str] = {node_id: "approved"}
    for d in descendants(nodes, node_id):
        if by_id[d].status != "rejected":
            changes[d] = "approved"
    return changes


def cascade_reject(nodes: Sequence[Node], node_id: str) -> dict[str, str]:
    """Reject a node and its whole subtree — a rejected node's descendants can
    never enact."""
    return {node_id: "rejected", **{d: "rejected" for d in descendants(nodes, node_id)}}


@dataclass(frozen=True)
class EnactmentPlan:
    """What the executor will run: `enactable` approved leaves whose every
    prerequisite is satisfied, and `held` approved leaves blocked by an unmet
    prerequisite (never enacted, fail-closed)."""

    enactable: tuple[str, ...]
    held: tuple[str, ...]


def enactment_plan(nodes: Sequence[Node]) -> EnactmentPlan:
    """Partition the approved, not-yet-enacted leaves into enactable vs held by
    their dependencies. A leaf with no deps is enactable once approved; a leaf
    whose any dep is unmet (rejected/pending/held) is held."""
    by_id = {n.id: n for n in nodes}
    enactable: list[str] = []
    held: list[str] = []
    for n in nodes:
        if n.type != "leaf" or n.status != "approved":
            continue
        if all(by_id[d].status in _SATISFIED for d in n.deps if d in by_id):
            enactable.append(n.id)
        else:
            held.append(n.id)
    return EnactmentPlan(tuple(enactable), tuple(held))


# --- Staging shapes --------------------------------------------------------


@dataclass(frozen=True)
class NodeSpec:
    """One node to stage. `id` is client-assigned so `deps` can reference siblings
    before they exist in the DB."""

    id: str
    type: str
    op: str = ""
    label: str = ""
    preview: dict = field(default_factory=dict)
    deps: tuple[str, ...] = ()
    parent_id: str | None = None


@dataclass(frozen=True)
class ProposalSpec:
    kind: str
    domain: str
    title: str
    nodes: Sequence[NodeSpec]
    provenance: dict = field(default_factory=dict)
    subject_id: str | None = None
    session_id: str | None = None


# A leaf executor turns one approved+satisfied leaf into its real effect (e.g. an
# agent-authored note re-entering the pipeline). Injected, so the engine stays
# decoupled from what a kind actually does.
LeafExecutor = Callable[[SessionContext, "ProposalRow", "NodeRow"], Awaitable[None]]


@dataclass(frozen=True)
class ProposalRow:
    id: str
    kind: str
    status: str
    domain: str
    title: str
    subject_id: str | None


@dataclass(frozen=True)
class ProposalSummary:
    id: str
    kind: str
    status: str
    domain: str
    title: str
    node_count: int


@dataclass(frozen=True)
class NodeRow:
    id: str
    parent_id: str | None
    type: str
    op: str
    label: str
    preview: dict
    deps: tuple[str, ...]
    status: str

    def to_node(self) -> Node:
        return Node(self.id, self.parent_id, self.type, self.status, self.deps)


class ProposalRepo:
    """Staging + decision + enactment over RLS-scoped sessions. The owner-only,
    domain-narrowed RLS (migration 0018) is the firewall."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def stage(self, ctx: SessionContext, *, principal_id: str, spec: ProposalSpec) -> str:
        async with scoped_session(self._maker, ctx) as session:
            prop_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.proposals"
                        " (principal_id, session_id, kind, title, provenance, domain_code,"
                        "  subject_id)"
                        " VALUES (:pid, :sid, :kind, :title, cast(:prov AS jsonb), :domain, :subj)"
                        " RETURNING id"
                    ),
                    {
                        "pid": principal_id,
                        "sid": spec.session_id,
                        "kind": spec.kind,
                        "title": spec.title,
                        "prov": _json(spec.provenance),
                        "domain": spec.domain,
                        "subj": spec.subject_id,
                    },
                )
            ).scalar()
            for node in spec.nodes:
                await session.execute(
                    text(
                        "INSERT INTO app.proposal_nodes"
                        " (id, proposal_id, parent_id, type, op, label, preview, deps)"
                        " VALUES (cast(:id AS uuid), :prop, cast(:parent AS uuid), :type, :op,"
                        "  :label, cast(:preview AS jsonb), cast(:deps AS uuid[]))"
                    ),
                    {
                        "id": node.id,
                        "prop": str(prop_id),
                        "parent": node.parent_id,
                        "type": node.type,
                        "op": node.op,
                        "label": node.label,
                        "preview": _json(node.preview),
                        "deps": list(node.deps),
                    },
                )
        return str(prop_id)

    async def list_open(self, ctx: SessionContext) -> list[ProposalSummary]:
        """Staged/approved proposals awaiting the owner — the review inbox, newest
        first. RLS narrows to in-scope, owner-only proposals."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT p.id, p.kind, p.status, p.domain_code, p.title,"
                        " (SELECT count(*) FROM app.proposal_nodes n WHERE n.proposal_id = p.id)"
                        "   AS node_count"
                        " FROM app.proposals p WHERE p.status IN ('staged', 'approved')"
                        " ORDER BY p.created_at DESC"
                    )
                )
            ).all()
        return [
            ProposalSummary(str(r.id), r.kind, r.status, r.domain_code, r.title, r.node_count)
            for r in rows
        ]

    async def load(
        self, ctx: SessionContext, proposal_id: str
    ) -> tuple[ProposalRow, list[NodeRow]]:
        async with scoped_session(self._maker, ctx) as session:
            prow = (
                await session.execute(
                    text(
                        "SELECT id, kind, status, domain_code, title, subject_id"
                        " FROM app.proposals WHERE id = :id"
                    ),
                    {"id": proposal_id},
                )
            ).one_or_none()
            if prow is None:
                raise ValueError("no proposal with that id in scope")
            nrows = (
                await session.execute(
                    text(
                        "SELECT id, parent_id, type, op, label, preview, deps, status"
                        " FROM app.proposal_nodes WHERE proposal_id = :id"
                    ),
                    {"id": proposal_id},
                )
            ).all()
        proposal = ProposalRow(
            str(prow.id),
            prow.kind,
            prow.status,
            prow.domain_code,
            prow.title,
            str(prow.subject_id) if prow.subject_id else None,
        )
        nodes = [
            NodeRow(
                str(r.id),
                str(r.parent_id) if r.parent_id else None,
                r.type,
                r.op,
                r.label,
                dict(r.preview),
                tuple(str(d) for d in r.deps),
                r.status,
            )
            for r in nrows
        ]
        return proposal, nodes

    async def decide(self, ctx: SessionContext, node_id: str, *, approve: bool) -> None:
        """Approve or reject a node, cascading by containment over its subtree."""
        async with scoped_session(self._maker, ctx) as session:
            proposal_id = (
                await session.execute(
                    text("SELECT proposal_id FROM app.proposal_nodes WHERE id = :id"),
                    {"id": node_id},
                )
            ).scalar()
            if proposal_id is None:
                raise ValueError("no proposal node with that id in scope")
            nodes = await self._nodes(session, str(proposal_id))
            changes = (cascade_approve if approve else cascade_reject)(nodes, node_id)
            await self._apply(session, changes)

    async def enact(
        self, ctx: SessionContext, proposal_id: str, executor: LeafExecutor
    ) -> EnactmentPlan:
        """Run every enactable leaf through the executor and mark it enacted; mark
        held leaves held. Dependency-safe: a leaf with an unmet prerequisite is
        held, not enacted (#fail-closed)."""
        proposal, node_rows = await self.load(ctx, proposal_id)
        plan = enactment_plan([n.to_node() for n in node_rows])
        by_id = {n.id: n for n in node_rows}
        async with scoped_session(self._maker, ctx) as session:
            for leaf_id in plan.enactable:
                await executor(ctx, proposal, by_id[leaf_id])
            await self._apply(
                session,
                {**{i: "enacted" for i in plan.enactable}, **{i: "held" for i in plan.held}},
            )
            # The proposal is enacted once at least one leaf ran and none remain
            # pending/approved-but-unenacted.
            if plan.enactable:
                await session.execute(
                    text(
                        "UPDATE app.proposals SET status = 'enacted', updated_at = now()"
                        " WHERE id = :id"
                    ),
                    {"id": proposal_id},
                )
        return plan

    async def _nodes(self, session: AsyncSession, proposal_id: str) -> list[Node]:
        rows = (
            await session.execute(
                text(
                    "SELECT id, parent_id, type, status, deps FROM app.proposal_nodes"
                    " WHERE proposal_id = :id"
                ),
                {"id": proposal_id},
            )
        ).all()
        return [
            Node(
                str(r.id),
                str(r.parent_id) if r.parent_id else None,
                r.type,
                r.status,
                tuple(str(d) for d in r.deps),
            )
            for r in rows
        ]

    async def _apply(self, session: AsyncSession, changes: Mapping[str, str]) -> None:
        for node_id, status in changes.items():
            await session.execute(
                text("UPDATE app.proposal_nodes SET status = :s WHERE id = :id"),
                {"s": status, "id": node_id},
            )


def _json(value: dict) -> str:
    return json.dumps(value)
