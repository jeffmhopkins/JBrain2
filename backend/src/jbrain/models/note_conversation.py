"""Note-conversation ORM + repo (migration 0191).

A note conversation is an ordinary `app.agent_sessions` row (D1 of
docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md); `note_conversations` is the side table
that makes one a note conversation, and `note_conversation_tool_calls` is the durable
per-tool-call ledger the D3 chip renders and constraint 6's whole-conversation
`touched`/`projected` accumulator reads back. W1 shipped `CommitOutcome` as an
in-memory dataclass precisely because that accumulation has to outlive a turn.

`NoteConversationRepo` takes the caller's already-RLS-scoped `AsyncSession` (the
`ArchivistMemoryRepo`/`PlanRepo` idiom): the handler owns the transaction, so the
owner-only firewall is Postgres', not these methods'.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Text,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import update

from jbrain.models.core import Base

log = structlog.get_logger()

# The two states that hold the note: the partial unique index admits exactly one of
# these per note. `settled` and `failed` release it so a new pass can start.
LIVE_STATES = ("running", "waiting_on_owner")

# A HARD ceiling on one note turn's wall time, the `api/agent.py:_MAX_TURN_WALL_CLOCK_S`
# idea sized for this persona rather than for /chat. It lives here, beside the states,
# because it is what makes "how long may a conversation legitimately be `running`" a
# fact the lifecycle knows rather than a runner-local guess. Far below /chat's 7500s:
# that number is sized for a deep-research sub-agent fan, and this turn has no fan
# (`tree=None`, so the fan-out tools refuse) — it is one bounded ReAct pass over one
# note. Generous enough for a cold on-box model load plus a full 20-step chain.
NOTE_TURN_WALL_CLOCK = timedelta(minutes=30)

# A `running` conversation older than this belongs to a dead pass, and is reclaimed —
# `queue.STALE_LOCK`'s reclaim, for the other thing a SIGKILL can strand. It has to be
# reclaimable because `running` holds the note's ONE live slot: the dispatcher suppresses
# every future `note_converse` for that note while it stands, so a worker killed mid-turn
# (`Ops -> Update` quiesces with `docker compose stop -t 30 worker`) would silently take
# the note out of the pipeline forever, on a box with no terminal (CLAUDE.md #10).
#
# DERIVED from the turn cap, never a second free-standing number: a turn cannot outlive
# the cap, so twice the cap cannot reclaim a live pass, and the two cannot drift apart.
# `waiting_on_owner` is deliberately NOT reaped — it holds the owner's question and waits
# as long as the owner does; `_ALLOWED_SOURCES` makes dropping one say `abandon_question`.
STALE_CONVERSATION = 2 * NOTE_TURN_WALL_CLOCK

# Which state may follow which. Postgres' CHECK owns the closed SET of states (an
# unknown target falls through this table and is refused there, one authority); this
# table owns the EDGES, which a CHECK cannot see. The edge that matters is the one
# absent from `failed`/`settled`: a thread `waiting_on_owner` holds a question the
# notes tab (D4/D5) is pointing at, and the partial unique index only stops a RIVAL
# conversation — nothing stops a retry or a reaper REPLACING the state, which would
# make the question vanish from the inbox and release the note with no trace. That
# edge needs `abandon_question=True` said out loud.
_ALLOWED_SOURCES: dict[str, frozenset[str]] = {
    "running": frozenset({"running", "waiting_on_owner"}),
    "waiting_on_owner": frozenset({"running", "waiting_on_owner"}),
    # Constraint 6 sweeps only on a turn that ended cleanly and NOT awaiting the owner,
    # so `settled` is reachable from `running` alone. Terminal after that: a retry opens
    # a fresh conversation rather than reviving a finished one.
    "settled": frozenset({"running", "settled"}),
    "failed": frozenset({"running", "failed"}),
}

# The loop stop reason `ask_owner` ends a turn with (`agent/asktools.py`), and the ONLY
# producer of `waiting_on_owner` — the state W2 shipped with no producer at all. It lives
# here, beside the edges, because "which endings mean which state" is the same lifecycle
# question `_ALLOWED_SOURCES` answers, and because a stop reason defined in the tool
# module and a state machine defined here would drift apart the first time either moved.
AWAITING_OWNER = "awaiting_owner"

# A turn that reached its own end, as opposed to one `max_steps`, the cost budget or
# consecutive tool errors cut off partway.
CLEAN_STOP = "end_turn"


def state_for_stop(stop_reason: str) -> str:
    """The state a pass that ended for `stop_reason` lands in.

    Three outcomes and one rule behind them (constraint 6): `settled` is a claim that the
    pass finished and everything it meant to write is written, because the whole-note
    settle sweep retracts whatever `settled` does not vouch for. A turn cut off partway
    asserted only a prefix, and a turn that stopped to ask a question has not finished
    reading — neither may claim it, so both land somewhere the sweep does not run."""
    if stop_reason == AWAITING_OWNER:
        return "waiting_on_owner"
    return "settled" if stop_reason == CLEAN_STOP else "failed"


# Caps on a recorded call's `args`. The blob is stored, never executed — but a note body
# may be third-party text (risk 1) and the model copies note text into `quote` /
# `statement`, so an unbounded ledger is a disk-exhaustion lever a hostile body can pull
# on a box whose storage the owner cannot reclaim from a terminal. Truncation lives here
# rather than in a DB CHECK because a CHECK would abort the transaction that carries the
# graph write the call already made: losing the audit row is the lesser harm, losing the
# write is not.
MAX_ARG_CHARS = 2000
MAX_ARGS_CHARS = 16000
# Nesting beyond this collapses to a repr. A handler builds these dicts, so a cycle is
# reachable and a RecursionError here would cost the audit row the caps exist to keep.
MAX_ARG_DEPTH = 12
# Set on a capped blob so a reader never mistakes a truncated argument for what the model
# actually sent. Overwrites a same-named model argument, which is the safe direction.
TRUNCATED_KEY = "_truncated"

# The owner-knowledge domains a graph write can land in. `app.domains` also seeds the
# corpus-only `external` (0136) and `jmolt` (0172), which notes, extraction and the wiki
# deliberately exclude — a ledger row records a graph write, so it uses the same
# four-domain allow-list `analysis/extraction.py` does (a unit test pins them equal).
# Validated here rather than by a trigger: a trigger would abort the transaction
# carrying the graph write the call already made, which is the same trade the size cap
# makes. Unknown codes are REFUSED rather than stored, because the handler fills this
# from what the write path reported — an unrecognised code is a bug in this repo, not
# untrusted input, and storing it would make the ledger lie about where a write landed.
KNOWLEDGE_DOMAINS = frozenset({"general", "health", "finance", "location"})


class InvalidStateTransition(ValueError):
    """A state edge `_ALLOWED_SOURCES` refuses. Loud rather than silent: the edge this
    exists for drops the owner's question out of the notes tab."""


def note_body_sha(body: str) -> str:
    """The `note_body_sha` a conversation is opened against. D6 appends clarification
    blocks and that re-ingests the note, so a resumed pass can compare this against the
    live body to learn the note moved under it. `analysis/clarify.py` is that reader: on
    the owner's reply it compares this against the note's composed text, and re-stamps
    the field only when the two still agree."""
    return hashlib.sha256(body.encode()).hexdigest()


def _cap_str(value: str, flag: list[bool]) -> str:
    if len(value) > MAX_ARG_CHARS:
        flag[0] = True
        return value[:MAX_ARG_CHARS]
    return value


def _truncate(value: Any, flag: list[bool], depth: int = 0) -> Any:
    """Bound one value AND make it JSONB-safe. Every branch returns something
    `json.dumps` accepts, because SQLAlchemy's JSONB bind processor serializes with a
    bare `json.dumps` (no `json_serializer` is set on any engine here) — a `UUID` or a
    `datetime` reaching it raises inside the flush and takes down the transaction
    carrying the graph write, which is exactly the abort the caps exist to avoid. So the
    fallthrough coerces with `str`, and it is a fallthrough rather than a list of known
    types on purpose."""
    if isinstance(value, str):
        return _cap_str(value, flag)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        # jsonb takes an arbitrary-precision integer; the total cap catches a huge one.
        return value
    if isinstance(value, float):
        # NaN/Infinity serialize to bare tokens `jsonb` rejects outright.
        return value if math.isfinite(value) else _cap_str(repr(value), flag)
    if depth >= MAX_ARG_DEPTH:
        flag[0] = True
        return _cap_str(repr(value), flag)
    if isinstance(value, Mapping):
        # Keys too: a single 200 KB key is otherwise an unbounded ledger row, and two
        # keys that collide once capped fold into one, which the `_truncated` mark
        # already warns about.
        return {_cap_str(str(k), flag): _truncate(v, flag, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_truncate(v, flag, depth + 1) for v in value]
    return _cap_str(str(value), flag)


def cap_tool_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Bound one call's recorded arguments, and return something the JSONB bind
    processor always accepts. Strings and KEYS are truncated in place at any nesting
    depth, because the batch shapes in TOOL_SURFACE.md put the quotes inside arrays of
    objects. The result is then bounded unconditionally, in two more steps rather than
    one: a blob still over the total cap — many small elements clear the per-string cap
    and can still be huge — degrades to its (capped) key names, and a blob whose key
    names alone are still over cap degrades to their count. The chip wants the shape of
    the call; the ledger only has to record that the call happened."""
    flag = [False]
    capped: dict[str, Any] = _truncate(dict(args), flag)
    # A model-supplied `_truncated` must not survive to claim a truncation that did not
    # happen; the flag below is the only writer of this key.
    capped.pop(TRUNCATED_KEY, None)
    if len(json.dumps(capped)) > MAX_ARGS_CHARS:
        flag[0] = True
        capped = {"_keys": sorted(capped)}
        if len(json.dumps(capped)) > MAX_ARGS_CHARS:
            capped = {"_key_count": len(args)}
    if flag[0]:
        capped[TRUNCATED_KEY] = True
    return capped


def validate_domains(domains: Sequence[str]) -> list[str]:
    """Refuse a domain code `app.domains` does not know. The column is a plain `text[]`
    (Postgres has no per-element array FK) and a validating trigger would abort the
    graph write, so the contract is kept here — a docstring is not one."""
    unknown = sorted(set(domains) - KNOWLEDGE_DOMAINS)
    if unknown:
        raise ValueError(f"unknown domain code(s) for a note-conversation ledger row: {unknown}")
    return list(domains)


class NoteConversation(Base):
    """One note's ingest conversation: the session it runs in, the note it reads, where
    it is in its lifecycle, and the body it was started against."""

    __tablename__ = "note_conversations"
    __table_args__ = {"schema": "app"}

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.agent_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.notes.id", ondelete="CASCADE")
    )
    state: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    note_body_sha: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoteConversationToolCall(Base):
    """One recorded tool call of a note conversation — what the write CLAIMED, in call
    order. Append-only apart from `turn_id`, which is bound when the assistant turn is
    written at the end of the exchange."""

    __tablename__ = "note_conversation_tool_calls"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.note_conversations.session_id", ondelete="CASCADE"),
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.agent_turns.id", ondelete="SET NULL"), nullable=True
    )
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    name: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")
    entity_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    fact_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    # No default, in the column or here: the writer states where the write landed, even
    # when the answer is "nowhere" (0191's docstring).
    domains: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# How much of a note the inbox row quotes. The row is a REDIRECT (D4) — enough to
# recognise which note raised the question, never enough to answer it here.
NOTE_EXCERPT_CHARS = 160


@dataclass(frozen=True)
class NotesInboxEntry:
    """One note-conversation row of the review inbox's notes tab (D4/D5). Read-only and
    decision-free by construction: it carries what a redirect needs to be worth taking —
    which note, what is being asked, how long it has waited, how much already landed —
    and no id or verb any answer could be posted against."""

    session_id: str
    # The session's persona, so the PWA flips to the conversation tab that hosts it
    # before opening the thread — a redirect that lands on the wrong tab shows an empty
    # chat, which reads as "the question is gone".
    agent: str
    note_id: str
    domain: str
    note_excerpt: str
    captured_at: datetime
    question: str | None
    waiting_since: datetime
    committed: int
    # A first pass still `running` is LISTED but not counted: the agent is reading, and
    # nothing is waiting on the owner yet (the mock's uncounted trailing row).
    live: bool


@dataclass(frozen=True)
class ConversationWrites:
    """The whole-conversation union of what its successful calls wrote — constraint 6's
    `touched`/`projected` sets, durable across turns. `settle_note` retracts every
    non-pinned fact of the note NOT in `facts`, so a per-turn share would retract the
    previous turn's commits; this is why the ledger exists.

    `frozenset`, not `set`: `frozen=True` only stops the FIELDS being rebound, and a
    caller that dropped an id from a mutable `facts` would silently widen the sweep.

    **`facts` IS FILLED FOR ONE TURN PATH AND EMPTY FOR THE OTHER, AND AN EMPTY `facts`
    IS NOT "NOTHING WAS WRITTEN".** `record_tool_call` has exactly two callers: the
    worker's unattended pass (`analysis/converse.py`, via `ledger_rows`, which does now
    report real fact ids) and `ask_owner`'s self-record. The owner's REPLY turn is an
    ordinary `/chat` turn — `api/agent.py` touches this repo nowhere — so a
    `resolve_entity` / `assert_fact` / `correct_fact` on that turn reaches the D3 rung
    through the transcript and never reaches this table.

    That asymmetry is the trap, and it is worse than the old always-empty state because
    it looks solved. `clarify.close_owner_reply` maps that turn's clean end to
    `settled`; wiring `settle_note(touched=writes().facts)` off `settled` — which is
    exactly what constraint 6 specifies — would retract every unpinned fact the owner's
    own reply just added, while the transcript still shows them recorded. `correct_fact`
    survives only by accident, because it pins. **W4 must move the recorder into the
    tool dispatch, or scope the sweep to the unattended pass, BEFORE wiring it.**"""

    facts: frozenset[uuid.UUID] = field(default_factory=frozenset)
    """The fact ids the conversation's successful calls wrote. Filled by the unattended
    pass; EMPTY for anything the owner's reply turn wrote — see the class docstring
    before feeding this to `settle_note`."""

    entities: frozenset[uuid.UUID] = field(default_factory=frozenset)
    domains: frozenset[str] = field(default_factory=frozenset)


class NoteConversationRepo:
    """Reads/writes a note conversation and its tool-call ledger on a caller-supplied
    RLS-scoped session."""

    # --- the conversation ------------------------------------------------------

    async def start(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        note_id: str,
        body_sha: str,
        state: str = "running",
    ) -> NoteConversation:
        """Open a conversation for a note. Raises `IntegrityError` when the note already
        has a live one — the partial unique index, not a read-then-write check, is what
        makes that race-free.

        `session_id` must be a session opened FOR this note. Deleting the note deletes
        that `agent_sessions` row whole (`analysis/purge.py:_purge_conversations`),
        because the transcript holds the note's body — so pointing this at an existing
        Full Brain chat would hand that chat's whole history to the note's purge. D1's
        "same agent, same loop, same memory" is about the agent, not about reusing a
        session row.
        """
        if state not in LIVE_STATES:
            # A conversation opens live or not at all: one opened straight into
            # `settled` would release the note it never read.
            raise InvalidStateTransition(f"a conversation opens live, not in {state!r}")
        stmt = (
            insert(NoteConversation)
            .values(
                session_id=uuid.UUID(session_id),
                note_id=uuid.UUID(note_id),
                note_body_sha=body_sha,
                state=state,
            )
            .returning(NoteConversation)
        )
        return (await session.execute(stmt)).scalar_one()

    async def get(self, session: AsyncSession, session_id: str) -> NoteConversation | None:
        return await session.get(NoteConversation, uuid.UUID(session_id))

    async def reclaim_stale(
        self,
        session: AsyncSession,
        *,
        note_id: str | None = None,
        horizon: timedelta = STALE_CONVERSATION,
    ) -> list[uuid.UUID]:
        """Fail every `running` conversation whose last transition is older than
        `horizon`, and return the sessions reclaimed. Scoped to one note when asked.

        A pass can only leave `running` from inside the handler that opened it, and a
        SIGKILL between the two is not exotic: `Ops -> Update` quiesces the worker with
        `docker compose stop -t 30 worker` (`deploy/update-inner.sh`), so any note being
        read when the owner updates the box lands here. Without a reclaim that note is
        suppressed by `_already_active` forever, silently, and the owner has no terminal
        to clear it with.

        `running` only: `waiting_on_owner` is a question the owner has not answered yet,
        and reaping it would drop that question out of the notes tab (D4/D5) — the very
        edge `_ALLOWED_SOURCES` makes callers spell `abandon_question` for.

        The cutoff is Postgres' own clock (`now()`), like `queue.claim`'s, so a skewed
        app clock can never widen it."""
        cutoff = text("now() - make_interval(secs => :stale_secs)").bindparams(
            stale_secs=horizon.total_seconds()
        )
        stmt = (
            update(NoteConversation)
            .where(
                NoteConversation.state == "running",
                NoteConversation.updated_at < cutoff,
            )
            .values(state="failed", updated_at=func.now())
            .returning(NoteConversation.session_id)
        )
        if note_id is not None:
            stmt = stmt.where(NoteConversation.note_id == uuid.UUID(note_id))
        return list((await session.execute(stmt)).scalars())

    async def live_for_note(self, session: AsyncSession, note_id: str) -> NoteConversation | None:
        """The note's live thread, or None. At most one exists by construction.

        Reclaims this note's abandoned `running` passes first, `queue.claim`'s shape:
        the reclaim is fused into the read it would otherwise block, so there is one
        place to get it right rather than one per caller who remembers. Both gates that
        can strand a note run through here — the handler's own pre-check and the
        dispatcher's `_has_live_conversation`."""
        reclaimed = await self.reclaim_stale(session, note_id=note_id)
        if reclaimed:
            log.warning(
                "note_conversation.reclaimed_stale",
                note_id=note_id,
                sessions=[str(s) for s in reclaimed],
            )
        stmt = select(NoteConversation).where(
            NoteConversation.note_id == uuid.UUID(note_id),
            NoteConversation.state.in_(LIVE_STATES),
        )
        return (await session.execute(stmt)).scalars().first()

    async def list_in_state(
        self, session: AsyncSession, state: str, *, limit: int = 50
    ) -> list[NoteConversation]:
        """Conversations in one state, most recently TRANSITIONED first — the notes inbox
        tab's query for `waiting_on_owner` (D4/D5), served by the partial index. Ordering
        is by `updated_at`, which only a state change bumps: a recorded tool call is not
        a reason to reorder the owner's list of questions, and making the ledger touch
        the parent row would cost an UPDATE per call for it."""
        stmt = (
            select(NoteConversation)
            .where(NoteConversation.state == state)
            .order_by(NoteConversation.updated_at.desc(), NoteConversation.session_id.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars())

    async def notes_inbox(self, session: AsyncSession, *, limit: int = 50) -> list[NotesInboxEntry]:
        """The notes tab of the review inbox (D4/D5): every live conversation, oldest
        wait first, with what it is asking and what it already committed.

        Both live states, not just `waiting_on_owner`: a first pass still `running` is
        listed so the owner can see the note is being read, and the route leaves it out
        of the count because nothing is waiting on them yet.

        The question is the LAST `ask_owner` of the thread — a conversation resumed after
        an answer can ask again, and the inbox must point at the open one, not the
        answered one. `committed` counts distinct fact ids over the thread's SUCCEEDED
        calls, so it is honestly 0 until the recorder moves into the tool dispatch
        (`ConversationWrites`' docstring) rather than a number invented from arguments.

        A soft-deleted note is excluded: `notes/repo.py`'s delete is soft, so its
        conversation survives, and a redirect into a deleted note's thread is a dead end.
        """
        rows = (
            await session.execute(
                text(
                    "SELECT c.session_id, c.note_id, c.state, c.updated_at, s.agent,"
                    " n.domain_code, n.body, n.created_at AS captured_at,"
                    " (SELECT t.args->>'question'"
                    "    FROM app.note_conversation_tool_calls t"
                    "   WHERE t.session_id = c.session_id AND t.name = 'ask_owner'"
                    "   ORDER BY t.seq DESC LIMIT 1) AS question,"
                    " (SELECT count(DISTINCT f) FROM app.note_conversation_tool_calls t2,"
                    "         unnest(t2.fact_ids) AS f"
                    "   WHERE t2.session_id = c.session_id AND t2.ok) AS committed"
                    "  FROM app.note_conversations c"
                    "  JOIN app.notes n ON n.id = c.note_id"
                    "  JOIN app.agent_sessions s ON s.id = c.session_id"
                    " WHERE c.state = ANY(:states) AND n.deleted_at IS NULL"
                    " ORDER BY c.updated_at ASC, c.session_id ASC"
                    " LIMIT :limit"
                ),
                {"states": list(LIVE_STATES), "limit": limit},
            )
        ).all()
        return [
            NotesInboxEntry(
                session_id=str(r.session_id),
                agent=r.agent,
                note_id=str(r.note_id),
                domain=r.domain_code,
                note_excerpt=_excerpt(r.body),
                captured_at=r.captured_at,
                question=r.question,
                waiting_since=r.updated_at,
                committed=int(r.committed or 0),
                live=r.state == "running",
            )
            for r in rows
        ]

    async def set_state(
        self,
        session: AsyncSession,
        session_id: str,
        state: str,
        *,
        abandon_question: bool = False,
    ) -> NoteConversation | None:
        """Transition a conversation. Returns None when it is gone, and raises
        `InvalidStateTransition` on an edge `_ALLOWED_SOURCES` refuses. An unknown state
        has no row in that table, so it is not filtered here and the DB CHECK refuses it
        — Postgres stays the one authority on the closed SET, this repo on its EDGES.

        `abandon_question=True` is the named override for the one edge worth naming,
        `waiting_on_owner -> failed`: it drops the owner's question out of the notes tab
        and releases the note, so a reaper or a retry has to say it means to."""
        allowed = _ALLOWED_SOURCES.get(state)
        if allowed is not None and abandon_question and state == "failed":
            allowed = allowed | {"waiting_on_owner"}
        stmt = (
            update(NoteConversation)
            .where(NoteConversation.session_id == uuid.UUID(session_id))
            .values(state=state, updated_at=func.now())
            .returning(NoteConversation)
            .execution_options(populate_existing=True)
        )
        if allowed is not None:
            stmt = stmt.where(NoteConversation.state.in_(sorted(allowed)))
        moved = (await session.execute(stmt)).scalar_one_or_none()
        if moved is not None:
            return moved
        current = await self.get(session, session_id)
        if current is None:
            return None
        raise InvalidStateTransition(
            f"{current.state!r} -> {state!r} is not a note-conversation transition"
        )

    async def set_body_sha(
        self, session: AsyncSession, session_id: str, body_sha: str
    ) -> NoteConversation | None:
        """Re-stamp the body this conversation stands on. Returns None when it is gone.

        The one legitimate caller is the owner-reply path (`analysis/clarify.py`), and
        only in the case where the stored sha still MATCHED before the append: the answer
        the reply appends is text the thread itself holds — the question was asked in it
        and the answer was typed into it — so a conversation that had read the note as it
        stood has read the note as it now stands, and leaving the old sha would report
        "the note moved under me" about this thread's own answer. When the sha did NOT
        match, nothing here is called and the stale value stands, which is exactly the
        true statement: something the thread never saw changed the note.

        Deliberately not folded into `set_state`: a state change is a lifecycle fact and
        a body sha is a claim about what was read, and the only caller that has grounds
        to make the second is not the many callers that make the first."""
        stmt = (
            update(NoteConversation)
            .where(NoteConversation.session_id == uuid.UUID(session_id))
            .values(note_body_sha=body_sha)
            .returning(NoteConversation)
            .execution_options(populate_existing=True)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    # --- the tool-call ledger --------------------------------------------------

    async def record_tool_call(
        self,
        session: AsyncSession,
        session_id: str,
        *,
        name: str,
        args: Mapping[str, Any] | None = None,
        ok: bool,
        domains: Sequence[str],
        detail: str = "",
        entity_ids: Iterable[uuid.UUID | str] = (),
        fact_ids: Iterable[uuid.UUID | str] = (),
    ) -> NoteConversationToolCall:
        """Record one call as it happens — before the assistant turn exists, hence no
        `turn_id`. `entity_ids`/`fact_ids`/`domains` are what the WRITE PATH reported,
        never what the model asked for: the ledger's job is to say what landed.

        `domains` has no default, in this signature or in the column: a call that wrote
        nothing passes `()` and says so."""
        stmt = (
            insert(NoteConversationToolCall)
            .values(
                session_id=uuid.UUID(session_id),
                name=name,
                args=cap_tool_args(args or {}),
                ok=ok,
                detail=detail,
                entity_ids=[_as_uuid(e) for e in entity_ids],
                fact_ids=[_as_uuid(f) for f in fact_ids],
                domains=validate_domains(domains),
            )
            .returning(NoteConversationToolCall)
        )
        return (await session.execute(stmt)).scalar_one()

    async def bind_turn(
        self,
        session: AsyncSession,
        session_id: str,
        turn_id: str,
        *,
        call_ids: Iterable[uuid.UUID | str],
    ) -> None:
        """Bind the calls of ONE exchange to the assistant turn just written (the
        `turn_attachments` idiom), named by the ids `record_tool_call` returned.

        Not "every unbound row": that is only correct if every turn ends in an assistant
        turn, and constraint 6 names the case where one does not — a turn cut off by
        `max_steps` or by consecutive tool errors (`loop.py:131-133`) asserted a prefix
        and wrote no turn, leaving its calls unbound forever. The next exchange would
        then adopt them and the D3 chip would render that write under the wrong
        exchange. A `seq` watermark has the same hole unless the caller carries a
        per-turn high-water mark, which is the same bookkeeping as carrying the ids the
        caller already holds. `session_id` stays in the predicate so an id from another
        conversation cannot be bound through this."""
        ids = [_as_uuid(c) for c in call_ids]
        if not ids:
            return
        await session.execute(
            update(NoteConversationToolCall)
            .where(
                NoteConversationToolCall.session_id == uuid.UUID(session_id),
                NoteConversationToolCall.id.in_(ids),
            )
            .values(turn_id=uuid.UUID(turn_id))
        )

    async def tool_calls(
        self, session: AsyncSession, session_id: str
    ) -> list[NoteConversationToolCall]:
        """This conversation's ledger in call order — what the D3 chip renders. Scoped to
        the one session: owner-only RLS is the table's firewall, so the session predicate
        is what keeps one note's thread from reading another's (0191's docstring)."""
        stmt = (
            select(NoteConversationToolCall)
            .where(NoteConversationToolCall.session_id == uuid.UUID(session_id))
            .order_by(NoteConversationToolCall.seq)
        )
        return list((await session.execute(stmt)).scalars())

    async def writes(self, session: AsyncSession, session_id: str) -> ConversationWrites:
        """The accumulated `touched`/`projected` sets for `settle_note`, for THIS session
        only. FAILED calls are excluded: a call that errored asserted nothing, and
        counting its ids would spare a fact the whole-note sweep is supposed to
        retract.

        **The returned `facts` covers the unattended pass ONLY.** The owner's reply turn
        runs on `/chat`, which never calls `record_tool_call`, so anything it wrote is
        missing here — not because the conversation wrote nothing. `settle_note`
        retracts every non-pinned fact of the note that is NOT in `touched`
        (`analysis/pipeline.py`), so passing this straight through would retract exactly
        the facts the owner's own answer added. Read `ConversationWrites`' docstring
        before you wire it."""
        stmt = select(
            NoteConversationToolCall.fact_ids,
            NoteConversationToolCall.entity_ids,
            NoteConversationToolCall.domains,
        ).where(
            NoteConversationToolCall.session_id == uuid.UUID(session_id),
            NoteConversationToolCall.ok.is_(True),
        )
        facts: set[uuid.UUID] = set()
        entities: set[uuid.UUID] = set()
        domains: set[str] = set()
        for row_facts, row_entities, row_domains in (await session.execute(stmt)).all():
            facts.update(row_facts or ())
            entities.update(row_entities or ())
            domains.update(row_domains or ())
        return ConversationWrites(
            facts=frozenset(facts), entities=frozenset(entities), domains=frozenset(domains)
        )


def _excerpt(body: str) -> str:
    """The note as the inbox row quotes it — one line, capped. Collapsed to a single
    line here rather than in CSS: the row is a two-line quote in the mock, and a note
    whose first line is blank would otherwise quote nothing at all."""
    line = " ".join(body.split())
    return line if len(line) <= NOTE_EXCERPT_CHARS else f"{line[:NOTE_EXCERPT_CHARS]}…"


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
