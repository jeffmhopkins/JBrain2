"""The Proposal engine: staging, dependency-safe partial approval, and enactment
(docs/reference/ASSISTANT.md "Staging & approval").

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


# The intake-link config fields the owner MAY edit on a staged Proposal (§7). subject_id
# and domain are deliberately absent — they are fixed by the agent's scope-checked staging
# and re-validated at mint, so an edit can never cross a firewall.
_EDITABLE_INTAKE_FIELDS = frozenset(
    {
        "opening_blurb",
        "label",
        "persona_brief",
        "fields_brief",
        "max_runs",
        "max_opens",
        "bind_on_first",
        "ttl_hours",
        "capture_enterer_name",
        "disclose_owner_identity",
    }
)


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
    # The Full Brain chat this proposal was staged from (None for background/system
    # ones). Surfaced so an enact can route its outcome back to the originating chat.
    session_id: str | None = None


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
    # The owner's free-text reason when this node was declined (None otherwise) —
    # owner-eyes feedback, folded into the enact outcome the assistant sees.
    decision_note: str | None = None

    def to_node(self) -> Node:
        return Node(self.id, self.parent_id, self.type, self.status, self.deps)


def enact_outcome_summary(
    proposal: ProposalRow, nodes: Sequence[NodeRow], plan: EnactmentPlan
) -> str:
    """A server-authored, human+agent-readable summary of what an enact did —
    built from DB truth (which leaves actually ran, which were owner-corrected,
    which were declined and why), never model text. It is the artefact the
    assistant receives after an inline enact, so it is honest by construction."""
    leaves = [n for n in nodes if n.type == "leaf"]
    enactable, held = set(plan.enactable), set(plan.held)
    enacted = [n for n in leaves if n.id in enactable]
    held_leaves = [n for n in leaves if n.id in held]
    declined = [n for n in leaves if n.status == "rejected"]
    corrected = [n for n in enacted if n.preview.get("edited")]
    plain = [n for n in enacted if not n.preview.get("edited")]

    def short(n: NodeRow) -> str:
        return (n.label or n.op or "operation").strip()

    if not enacted:
        head = f"Enacted nothing from “{proposal.title}”"
    else:
        parts: list[str] = []
        if plain:
            parts.append(f"{len(plain)} approved")
        if corrected:
            parts.append(f"{len(corrected)} corrected ({', '.join(short(n) for n in corrected)})")
        head = f"Enacted {len(enacted)} of {len(leaves)} — {', '.join(parts)}"
    tail = ""
    if declined:
        items = "; ".join(
            f"{short(n)}: {n.decision_note}" if n.decision_note else short(n) for n in declined
        )
        tail += f" · declined {len(declined)} ({items})"
    if held_leaves:
        tail += f" · {len(held_leaves)} held, not run"
    count = len(enacted)
    return f"{head}{tail}. Returned to assistant as {count} approval{'' if count == 1 else 's'}."


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

    async def list_open(
        self, ctx: SessionContext, session_id: str | None = None
    ) -> list[ProposalSummary]:
        """Staged/approved proposals awaiting the owner — the review inbox, newest
        first. RLS narrows to in-scope, owner-only proposals.

        When `session_id` is given, the inbox is the Full Brain chat's: its own
        chat-staged proposals plus the session-less (background/system) ones, which
        belong to no chat and so surface in every session's inbox. Omit it for the
        unscoped, see-everything list."""
        scoped = (
            " AND (p.session_id = cast(:sid AS uuid) OR p.session_id IS NULL)"
            if session_id is not None
            else ""
        )
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT p.id, p.kind, p.status, p.domain_code, p.title,"
                        " (SELECT count(*) FROM app.proposal_nodes n WHERE n.proposal_id = p.id)"
                        "   AS node_count"
                        " FROM app.proposals p WHERE p.status IN ('staged', 'approved')"
                        f"{scoped}"
                        " ORDER BY p.created_at DESC"
                    ),
                    {"sid": session_id},
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
                        "SELECT id, kind, status, domain_code, title, subject_id, session_id"
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
                        "SELECT id, parent_id, type, op, label, preview, deps, status,"
                        " decision_note FROM app.proposal_nodes WHERE proposal_id = :id"
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
            str(prow.session_id) if prow.session_id else None,
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
                r.decision_note,
            )
            for r in nrows
        ]
        return proposal, nodes

    async def decide(
        self, ctx: SessionContext, node_id: str, *, approve: bool, reason: str | None = None
    ) -> None:
        """Approve or reject a node, cascading by containment over its subtree. A
        `reason` (only meaningful on a reject) is recorded on the explicitly-declined
        node as owner-eyes feedback — it does not cascade to the subtree, and an
        approve clears any prior note so a re-approved node carries none."""
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
            note = reason.strip() if (reason and not approve) else None
            await session.execute(
                text("UPDATE app.proposal_nodes SET decision_note = :n WHERE id = :id"),
                {"n": note or None, "id": node_id},
            )

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

    async def patch_intake_config(self, ctx: SessionContext, node_id: str, fields: dict) -> bool:
        """Edit the constrained config of a STAGED intake-link mint node — the net-new
        editable-Proposal surface (§7). Only the soft fields are patchable; subject and
        domain are NOT (they are re-validated at mint, so the owner can't edit the config
        to cross a firewall the agent's staged config couldn't). Owner-only RLS; no-op on
        an unknown id, a non-intake node, or an already-decided proposal."""
        allowed = {k: v for k, v in fields.items() if k in _EDITABLE_INTAKE_FIELDS}
        if not allowed:
            return False
        async with scoped_session(self._maker, ctx) as session:
            preview = (
                await session.execute(
                    text(
                        "SELECT n.preview FROM app.proposal_nodes n"
                        " JOIN app.proposals p ON p.id = n.proposal_id"
                        " WHERE n.id = :nid AND n.op = 'mint_intake_link' AND p.status = 'staged'"
                    ),
                    {"nid": node_id},
                )
            ).scalar_one_or_none()
            if preview is None:
                return False
            merged = {**dict(preview), **allowed}
            await session.execute(
                text("UPDATE app.proposal_nodes SET preview = cast(:p AS jsonb) WHERE id = :nid"),
                {"p": _json(merged), "nid": node_id},
            )
            return True

    async def patch_node_body(self, ctx: SessionContext, node_id: str, body: str) -> bool:
        """Correct-in-place: replace a STAGED note/appointment leaf's proposed body with
        the owner's edited text, and flag it `edited` so enactment attributes it to the
        human (provenance='human') — the #7 owner-correction channel. Guarded to
        `add_note`/`manage_appointment` leaves on a still-staged proposal; the firewall
        fields (subject/domain) are untouched, so an edit can only refine the text the
        owner will approve, never re-target it. Owner-only RLS; no-op on an unknown id,
        a wrong-op node, an already-decided proposal, or an empty body."""
        text_body = body.strip()
        if not text_body:
            return False
        async with scoped_session(self._maker, ctx) as session:
            preview = (
                await session.execute(
                    text(
                        "SELECT n.preview FROM app.proposal_nodes n"
                        " JOIN app.proposals p ON p.id = n.proposal_id"
                        " WHERE n.id = :nid AND n.op IN ('add_note', 'manage_appointment')"
                        "   AND p.status = 'staged'"
                    ),
                    {"nid": node_id},
                )
            ).scalar_one_or_none()
            if preview is None:
                return False
            merged = {**dict(preview), "body": text_body, "edited": True}
            await session.execute(
                text("UPDATE app.proposal_nodes SET preview = cast(:p AS jsonb) WHERE id = :nid"),
                {"p": _json(merged), "nid": node_id},
            )
            return True

    async def mark_enacted(self, ctx: SessionContext, proposal_id: str) -> None:
        """Mark a proposal (and its nodes) enacted without running a leaf executor — the
        intake-link mint path enacts via its own endpoint (which surfaces the show-once
        secret a leaf executor can't return)."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "UPDATE app.proposals SET status = 'enacted', updated_at = now() WHERE id = :id"
                ),
                {"id": proposal_id},
            )
            await session.execute(
                text("UPDATE app.proposal_nodes SET status = 'enacted' WHERE proposal_id = :id"),
                {"id": proposal_id},
            )

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
