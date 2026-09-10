"""The owner answers, and the answer becomes part of the note.

W3/T2b of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md — the second half of `ask_owner`.
W2 shipped the storage (`app.note_clarifications`, `SqlNotesRepo.append_clarification`)
with no caller, and the plan says in so many words that W3's ask is that caller. This is
where the caller lives, because D6's block is deliberately NOT a tool
(`TOOL_SURFACE.md`, "Verbs deliberately NOT tools": *the D6 clarification block (engine,
on every owner turn)*) — a model that had to remember to file the owner's answer would
sometimes not, and the answer would exist only as chat.

The chain, end to end:

1. the owner replies in the thread (an ordinary /chat turn — D8: the reply IS the on-reply
   turn, and the persona's reply-time tool set is a sibling's concern, not this module's);
2. `record_owner_reply` pairs that message with the question the ledger says the thread is
   waiting on, and appends it as a timestamped clarification block;
3. `append_clarification` enqueues `ingest_note` INSIDE its own transaction, so the note is
   re-chunked and re-embedded with the block in it (D7) and the graph re-derives from
   notes alone — a fact drawn from the answer has a real chunk of a real note to cite;
4. the conversation returns to `running` and the turn proceeds.

It also holds the note conversation's TOOL-CALL LEDGER — the fold, the recorder, and the
bind — because both turn paths need them and only one of the two can afford to import
`analysis/converse.py`. See the block comment above `SELF_RECORDED_TOOLS`.

Two things worth stating because they are not obvious:

**Why the state moves BEFORE the append.** They are separate transactions (the repo owns
the append's), so one of the two can land alone. Moving the state first means the failure
mode is a lost block — recoverable, because the answer is still in the thread and the
agent can ask again. The other order's failure mode is a `waiting_on_owner` thread whose
question was already answered and appended: the owner answers again, and the note gains
the same answer TWICE, as source text, in a corpus with no per-block eraser in the PWA.
Duplicated source text is the one of the two that cannot be undone from the owner's side.

**Why only text the OWNER TYPED may become a block.** Not every `/chat` turn carries owner
prose. `ChatRequest.proposal_outcome` and `.deferred_outcome` mark a turn whose `message` the
SERVER wrote — an enact summary ("Enacted 1 of 1 — 1 approved…"), a finished off-turn
analysis — framed as a DATA report rather than as something Jeff said, which is why
`_record_transcript` already declines to record one as a user turn. Filing one here would be
strictly worse than a cosmetic slip: it pairs machine text with the agent's open question,
appends the pair to the owner's own note as source text, re-ingests it so it becomes chunks,
embeddings and citable facts, and consumes the question (the `running` latch below) so the
owner's REAL answer can never be paired with it. That is the wrong-sentence-in-the-corpus
failure this module exists to prevent, so `owner_authored=False` returns before anything
moves: the thread stays `waiting_on_owner` and the question stays open, which is the truth.

**A note that moved under the thread.** `note_conversations.note_body_sha` is the sha of
the body the pass read, shipped in W2 with no reader — this is the reader. Compared here
against the note's composed text as it stands, it answers one question the reply path
genuinely has to ask: has anything OTHER than this conversation changed the note since it
was read? On a match, the answer is appended and the sha is re-stamped, because the thread
has seen every character of the new composed text (the question was asked in it and the
answer typed into it), so the field keeps meaning what it says. On a mismatch the block is
still appended — the owner answered the question that was asked, and that is true whatever
else happened to the note — but the stale sha is left standing, so the field goes on
saying "this thread has not read the note as it now stands", which is the only statement
that is true.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.agents import AgentProfile, narrow_for_emr
from jbrain.agent.asktools import ASK_OWNER_TOOL, latest_question
from jbrain.analysis.settle_owner import CONVERSATION
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.emr.ownership import emr_owned
from jbrain.models.agent import AgentTurn
from jbrain.models.note_conversation import (
    MAX_ARG_CHARS,
    SETTLED,
    NoteConversationRepo,
    note_body_sha,
    state_for_stop,
)
from jbrain.notes.service import NoteInfo, NotesRepo

if TYPE_CHECKING:  # `analysis/pipeline.py` drags the LLM stack; only the TYPE is needed
    from jbrain.analysis.pipeline import AnalysisPipeline

log = structlog.get_logger()

NOTE_CONVERSE_AGENT = "note_ingest"
"""The persona whose sessions are note conversations. Spelled here rather than imported
from `analysis/converse.py`, which drags the whole turn runner (and through it the LLM
stack) into the API process for the sake of one string."""


# --- the tool-call ledger, shared by BOTH turn paths -------------------------
#
# It lives HERE, beside `close_owner_reply`, and not in `analysis/converse.py`, for the
# same reason `NOTE_CONVERSE_AGENT` is spelled above: `api/agent.py` is the reply turn's
# seam and importing the worker's turn runner into the API process to reach a fold and
# two inserts is not a trade worth making. `converse.py` imports these instead.
#
# Why the recorder sits at the TURN seam on both paths rather than inside the shared tool
# dispatch — the direction W2 left open, and the one W4c did not take. The dispatch serves
# every agent and knows nothing of note conversations, so recording there means threading
# a note-conversation hook through every tool that could ever run in one. Recording at the
# turn seam instead keeps the two paths SYMMETRIC: the same `ledger_rows` fold, the same
# `record_tool_call` loop, the same bind-by-run-id, on the unattended pass and the owner's
# reply alike — rather than making the reply turn stricter than the pass it continues. The
# non-atomicity that buys (rows can land with no turn to bind to, or a write can land with
# no row) is not a new failure mode: `converse._record` already reasons about exactly that
# reachable partial, and both paths answer it the same way — a ledger that did not land
# means the conversation does not reach `settled`, so constraint 6's sweep never reads a
# half-written ledger.

SELF_RECORDED_TOOLS = frozenset({ASK_OWNER_TOOL})
"""Tools that write their OWN ledger row, inside the transaction that carries the change
the row records. `ask_owner` is the first: its question has to be durable at ask time,
because the owner can reply before the turn's post-hoc recorder ever runs, and the reply
path reads that row to know what it is answering."""


@dataclass(frozen=True)
class LedgerRow:
    """One tool call as the ledger records it — what the call CLAIMED, in call order."""

    name: str
    args: dict[str, Any]
    ok: bool
    detail: str
    entity_ids: tuple[str, ...]
    domains: tuple[str, ...]
    # The fact rows the call WROTE (empty for every read tool, and for a write that
    # landed nothing). Constraint 6's `touched` set is the union of these.
    fact_ids: tuple[str, ...] = ()


def ledger_rows(tool_steps: Sequence[Mapping[str, Any]]) -> list[LedgerRow]:
    """Fold a turn's `TranscriptAccumulator.tool_steps()` into ledger rows.

    Pure, and separately tested against a REAL accumulator fed a real tool event stream.

    `entity_ids`/`domains` come from the step's resolved-entity chips
    (`ToolOutcome.entities`) and `fact_ids` from its WRITE chips (`ToolOutput.facts` /
    `contracts.FactWriteRef`) — both reported by the write path itself, never inferred
    from what the model ASKED for: `resolve_entity`/`assert_fact` surface the rows they
    actually wrote, and a call that wrote nothing surfaces nothing. Constraint 6's
    settle sweep reads this back as `touched`, so the direction matters in both
    directions: an id here that did not land SPARES a fact the sweep should retract, and
    a landed id missing here RETRACTS a fact the note still says.

    A write's domain is unioned from both chips — a fact's domain is the floored and
    ratcheted one the write path chose, which can be strictly above its entity's.

    `detail` is capped for the same reason `args` is — a hostile body can drive a large
    tool summary onto a disk the owner cannot reclaim from a terminal (CLAUDE.md #10)."""
    rows: list[LedgerRow] = []
    for step in tool_steps:
        if step.get("name") in SELF_RECORDED_TOOLS:
            # Already on the ledger, written by the handler inside the transaction that
            # made the change it records (`agent/asktools.py`). Recording it again here
            # would give one ask two rows, and the reply path reads the NEWEST `ask_owner`
            # to build the clarification block — a duplicate is not just noise, it is a
            # second row that could outlive a rollback of the first. This skip is what
            # keeps a REPLY turn that ends in another question to one row as well: that
            # turn's `ask_owner` self-recorded on its way through, and then arrives here
            # again on `acc.tool_steps()`.
            continue
        entities = [e for e in step.get("entities", []) if isinstance(e, Mapping)]
        facts = [f for f in step.get("facts", []) if isinstance(f, Mapping)]
        ids = tuple(str(e["entity_id"]) for e in entities if e.get("entity_id"))
        fact_ids = tuple(str(f["fact_id"]) for f in facts if f.get("fact_id"))
        domains = tuple(
            sorted(
                {str(e["domain"]) for e in entities if e.get("domain")}
                | {str(f["domain"]) for f in facts if f.get("domain")}
            )
        )
        rows.append(
            LedgerRow(
                name=str(step.get("name", "")),
                args=dict(step.get("args") or {}),
                # `tool_steps()` settles an interrupted step to ok=False itself; the
                # `is True` keeps a malformed step out of the truthy `writes()` union.
                ok=step.get("ok") is True,
                detail=str(step.get("summary") or "")[:MAX_ARG_CHARS],
                entity_ids=ids,
                fact_ids=fact_ids,
                domains=domains,
            )
        )
    return rows


async def record_turn_writes(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    session_id: str,
    tool_steps: Sequence[Mapping[str, Any]],
) -> list[uuid.UUID]:
    """Record one turn's calls on the ledger, UNBOUND, and return their ids for
    `bind_turn_writes`.

    Unbound because the assistant turn may not exist yet: the unattended pass records
    before it writes the exchange, so a call is on the ledger as it happened rather than
    only if the turn that made it survived to be persisted. The reply path could bind in
    one step, but does not — sharing this seam is what keeps the ledger's shape identical
    on both paths, which is the property `NoteConversationRepo.writes()` unions over.

    Raises. Both callers decide what a failure means for their own state, and both answer
    it the same way: the conversation must not reach `settled` on a ledger that did not
    land."""
    rows = ledger_rows(tool_steps)
    if not rows:
        return []
    repo = NoteConversationRepo()
    call_ids: list[uuid.UUID] = []
    async with scoped_session(maker, ctx) as s:
        for row in rows:
            call = await repo.record_tool_call(
                s,
                session_id,
                name=row.name,
                args=row.args,
                ok=row.ok,
                detail=row.detail,
                entity_ids=row.entity_ids,
                fact_ids=row.fact_ids,
                domains=row.domains,
            )
            call_ids.append(call.id)
    return call_ids


async def bind_turn_writes(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    session_id: str,
    run_id: str,
    call_ids: Sequence[uuid.UUID],
) -> bool:
    """Bind the calls `record_turn_writes` just wrote to THIS run's assistant turn.

    Identified by an exact predicate, never by "the newest assistant row in the session"
    and never by "whatever is unbound". `record_exchange` stamps `run_id` on both rows it
    writes and a run writes one assistant turn, so `(session, run, assistant)` names
    exactly the exchange these calls belong to. A note session holds more than one
    exchange the moment the owner replies, and under that every ordering heuristic is a
    guess about which exchange a tool call came from — the D3 chip would render one
    turn's writes under another's answer. `scalar_one_or_none` says so out loud: two
    assistant turns for one run would be a fault in the transcript writer, and raising
    beats binding the ledger to a coin flip.

    Returns whether the binding landed. `False` — no assistant turn for this run — is a
    real outcome on both paths, because both persist the transcript best-effort: the rows
    stay on the ledger with a NULL `turn_id`, which costs the D3 chip its grouping and
    costs `writes()` nothing, since the sweep unions over the SESSION."""
    if not call_ids:
        return True
    async with scoped_session(maker, ctx) as s:
        turn_id = (
            await s.execute(
                select(AgentTurn.id).where(
                    AgentTurn.session_id == uuid.UUID(session_id),
                    AgentTurn.run_id == uuid.UUID(run_id),
                    AgentTurn.role == "assistant",
                )
            )
        ).scalar_one_or_none()
        if turn_id is None:
            return False
        await NoteConversationRepo().bind_turn(s, session_id, str(turn_id), call_ids=call_ids)
    return True


async def record_reply_writes(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    session_id: str,
    agent: str,
    run_id: str,
    tool_steps: Sequence[Mapping[str, Any]],
) -> bool:
    """Put the OWNER REPLY turn's tool calls on the ledger, the way the unattended pass
    puts its own there. Returns whether the ledger is complete for this turn.

    This is what makes `ConversationWrites.facts` a whole-CONVERSATION union rather than a
    whole-PASS one. The reply turn is an ordinary `/chat` turn, so before this existed a
    `resolve_entity` / `assert_fact` / `correct_fact` the owner's own answer prompted
    reached the graph and the D3 chip and never reached the ledger — and constraint 6's
    sweep retracts every unpinned fact of the note that is NOT in `touched`, so wiring it
    over that ledger would have retracted exactly those writes.

    Must run BEFORE `close_owner_reply`: the sweep fires on the state that call sets, so
    the ledger has to be complete before the state flips.

    Never raises — the owner's turn already happened, its writes already committed in
    their own transactions, and 500ing the response would neither un-write them nor
    recover the row. The FALSE return is what the cost of that suppression is paid with:
    an unrecorded write is a fact the sweep would retract, so a caller that cannot record
    must not let the conversation claim `settled` (see the call site in `api/agent.py`,
    which degrades the close to `record_failed` — the same `stop_reason` the unattended
    pass lands on when its own recorder fails)."""
    if agent != NOTE_CONVERSE_AGENT:
        return True
    try:
        call_ids = await record_turn_writes(
            maker, ctx, session_id=session_id, tool_steps=tool_steps
        )
    except Exception as exc:  # noqa: BLE001 — the owner's turn stands; the state degrades
        log.warning("note_reply.ledger_failed", session_id=session_id, error=repr(exc))
        return False
    if not call_ids:
        return True
    # The BIND is not part of that verdict: the rows are already on the ledger, so
    # `writes()` is complete whether or not they carry a `turn_id`. An unbound row costs
    # the D3 chip its grouping, and nothing costs the sweep a fact.
    bound = False
    with contextlib.suppress(Exception):
        bound = await bind_turn_writes(
            maker, ctx, session_id=session_id, run_id=run_id, call_ids=call_ids
        )
    log.info("note_reply.ledger_recorded", session_id=session_id, calls=len(call_ids), bound=bound)
    return True


@dataclass(frozen=True)
class OwnerReply:
    """What the reply path did with one owner message."""

    question: str
    """The question the thread was waiting on — "" when the state said it was waiting and
    the ledger held no recorded ask (a shape only a partial write can produce)."""

    clarified: bool
    """Whether the answer actually landed on the note as a block. False means the note is
    unchanged and no re-ingest was queued: the answer is in the thread and nowhere else."""

    note_moved: bool
    """Whether the note had changed under the conversation since it was read."""


async def reply_profile_for_session(
    maker: async_sessionmaker[AsyncSession],
    notes: NotesRepo,
    ctx: SessionContext,
    *,
    session_id: str,
    agent: str,
    profile: AgentProfile,
) -> AgentProfile:
    """Narrow a note conversation's ON-REPLY profile when the EMR importer owns its note
    (W4/D9, `ingest/emr/ownership.py`).

    `/chat` resolves the wide on-reply set through `agent_for_owner_reply`; this is the
    one subtraction W4 makes to it. It lives here rather than in the route because it
    needs the conversation row and the note behind it, which this module already reads —
    and because the route must be able to call it unconditionally: a non-note persona,
    an unknown session, or a note the importer does not own all return the profile
    unchanged.

    FAILS CLOSED, at every step: no conversation row, no note, a soft-deleted note, or a
    raised exception all narrow. This half shipped failing OPEN, on the reading that the
    narrowing is a correctness guard rather than a firewall; W4's merge flipped it, and
    the reason is the OTHER predicate. `thirdparty.conversation_is_third_party` asks the
    SAME two questions of the SAME two rows on this same turn and fails closed, so a note
    read that blips already narrows the turn — to the third-party set, which still holds
    `resolve_entity` and `assert_fact`. Failing open here meant a blip left the graph
    writes bound on precisely the notes where a write is unsupersedable: `correct_fact`
    at an empty address commits active + PINNED, and a pinned lab head makes every later
    import of that reading `held`. The cost of the closed direction is that one reply
    turn on an EMR note loses verbs it would not have been allowed to use anyway — the
    unattended pass narrowed on the same predicate, and the worker's per-note registry
    binds no write handler for such a note either way.
    """
    if agent != NOTE_CONVERSE_AGENT or profile.tools is None:
        return profile
    try:
        async with scoped_session(maker, ctx) as s:
            conversation = await NoteConversationRepo().get(s, session_id)
        if conversation is None:
            log.warning("note_reply.no_conversation_row_for_emr", session_id=session_id)
            return narrow_for_emr(profile)
        note = await notes.get_note(ctx, str(conversation.note_id))
    except Exception as exc:  # noqa: BLE001 — an unreadable note is a note we narrow for
        log.warning("note_reply.emr_check_failed", session_id=session_id, error=repr(exc))
        return narrow_for_emr(profile)
    if note is None:
        log.warning("note_reply.note_gone_for_emr", session_id=session_id)
        return narrow_for_emr(profile)
    if not emr_owned(note.domain, note.destination, [a.media_type for a in note.attachments]):
        return profile
    return narrow_for_emr(profile)


async def record_owner_reply(
    maker: async_sessionmaker[AsyncSession],
    notes: NotesRepo,
    ctx: SessionContext,
    *,
    session_id: str,
    agent: str,
    message: str,
    owner_authored: bool = True,
) -> OwnerReply | None:
    """Turn the owner's reply into a clarification block on the note. `None` when this
    message is not an answer to anything — not a note conversation, not waiting, not
    written by the owner, or empty.

    `owner_authored=False` for a turn whose `message` the SERVER composed (a proposal
    enact outcome, a deferred-tool result): it is a DATA report on the channel, not
    Jeff's answer, and the docstring above says why filing one is the worst thing this
    module could do. Defaulted True so a caller must say so deliberately, and checked
    here rather than only at the call site so the rule is the function's, not the
    caller's.

    Never raises: a reply that cannot be filed must still be a reply the agent can read,
    so every failure here degrades to "the block did not land" and the turn goes on.
    """
    if agent != NOTE_CONVERSE_AGENT:
        return None
    if not owner_authored:
        return None
    answer = message.strip()
    if not answer:
        # An attachment-only turn, say. Nothing to record as an answer, and the thread
        # stays `waiting_on_owner` — the question is still open, which is the truth.
        return None

    repo = NoteConversationRepo()
    try:
        async with scoped_session(maker, ctx) as s:
            conversation = await repo.get(s, session_id)
            if conversation is None or conversation.state != "waiting_on_owner":
                # An ordinary follow-up in a note thread that is not waiting on anything.
                # It is conversation, not an answer, and D6's block pairs a question with
                # an answer — `note_clarifications.question` is NOT NULL and non-blank in
                # Postgres, so there is no shape for "the owner said something unprompted"
                # even if it were wanted. The reply stands as chat.
                return None
            note_id = str(conversation.note_id)
            stored_sha = conversation.note_body_sha
            question = await latest_question(s, repo, session_id)
            # The question is consumed HERE, before the append: this transition is what
            # says "that question has been answered", and it is the latch that stops a
            # second reply appending the same answer again.
            await repo.set_state(s, session_id, "running")
    except Exception as exc:  # noqa: BLE001 — a reply the engine cannot file is still a reply
        log.warning("note_reply.claim_failed", session_id=session_id, error=repr(exc))
        return None

    if not question:
        # `ask_owner` writes the ledger row and the state in one transaction, so this is
        # unreachable short of a hand-edited row — but a block with a fabricated question
        # would be a sentence the owner never said, appended to their own note.
        log.warning("note_reply.no_recorded_question", session_id=session_id, note_id=note_id)
        return OwnerReply(question="", clarified=False, note_moved=False)

    moved = False
    try:
        current = await notes.get_note(ctx, note_id)
        moved = current is not None and note_body_sha(current.body) != stored_sha
        clarified = await notes.append_clarification(
            ctx, note_id, question=question, answer=answer, session_id=session_id
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("note_reply.append_failed", session_id=session_id, error=repr(exc))
        return OwnerReply(question=question, clarified=False, note_moved=moved)
    if clarified is None:
        # The note is gone (soft-deleted). The question cannot be answered onto it, and
        # the state is already back to `running`, so nothing holds the note's live slot.
        log.info("note_reply.note_gone", session_id=session_id, note_id=note_id)
        return OwnerReply(question=question, clarified=False, note_moved=moved)

    if not moved:
        with contextlib.suppress(Exception):
            async with scoped_session(maker, ctx) as s:
                await repo.set_body_sha(s, session_id, note_body_sha(clarified.body))
    log.info(
        "note_reply.clarified",
        session_id=session_id,
        note_id=note_id,
        note_moved=moved,
    )
    return OwnerReply(question=question, clarified=True, note_moved=moved)


async def close_owner_reply(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    *,
    session_id: str,
    agent: str,
    stop_reason: str,
    reopened: bool,
) -> str | None:
    """End the conversation the owner's reply re-opened, by the same rule the unattended
    pass ends by (`state_for_stop`). Returns the state it WROTE, or None when it wrote
    none — which is the caller's gate for `settle_conversation`, so the answer to "did
    this pass end cleanly?" is given by the call that decided it rather than re-derived
    beside it.

    `reopened` says whether THIS turn moved the thread `waiting_on_owner -> running`,
    which is exactly `record_owner_reply` returning an `OwnerReply`. It is required, and
    a `running` state is not a substitute for it: a conversation is `running` for the
    whole of the worker's unattended pass — up to `NOTE_TURN_WALL_CLOCK`, 30 minutes —
    and `/chat`'s busy guard counts only the API's own live turns, so nothing stops the
    owner opening the thread and typing while that pass is mid-flight. Closing on the
    state alone then declared a LIVE pass `settled` before its `_record` had written a
    single ledger row, and the settle behind this call swept the note against an empty
    ledger: the incomplete-ledger retraction the whole gate exists to prevent, reached
    without any of its three refusals firing. It also left the worker's own `set_state`
    raising `InvalidStateTransition` into a job retry, which opens a second thread for
    the note.

    Something has to: `record_owner_reply` put the thread back in `running`, and `running`
    holds the note's ONE live slot — the re-ingest the answer just queued emits its own
    `note.ingested`, and the pass that event opens is suppressed while this one stands. So
    a reply turn that never closed would leave the answered note un-re-read, until the
    stale-conversation reaper eventually called it `failed` an hour later.

    A turn the agent ended with another `ask_owner` is left exactly where the handler put
    it: the thread is waiting again, and `state_for_stop` says so."""
    if agent != NOTE_CONVERSE_AGENT or not reopened:
        return None
    state = state_for_stop(stop_reason)
    repo = NoteConversationRepo()
    try:
        async with scoped_session(maker, ctx) as s:
            conversation = await repo.get(s, session_id)
            if conversation is None or conversation.state != "running":
                return None
            await repo.set_state(s, session_id, state)
    except Exception as exc:  # noqa: BLE001 — the reaper is the backstop
        log.warning("note_reply.close_failed", session_id=session_id, error=repr(exc))
        return None
    log.info("note_reply.closed", session_id=session_id, state=state, stop_reason=stop_reason)
    return state


async def settle_conversation(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    pipeline: AnalysisPipeline,
    notes: NotesRepo,
    *,
    session_id: str,
    state: str,
) -> bool:
    """Run the note conversation's end-of-pass settle, and say whether it ran — the
    sweep that releases its claim (S3) and the tail that projects what it wrote (S2).

    The conversation's write path is `commit_facts` and nothing else
    (`agent/graphwritetools.py`), and `commit_facts` deliberately does nothing
    whole-note. Two consequences, and this closes both.

    **The tail.** Everything the graph DERIVES from a note's rows —
    `reproject_canonical_name`, the corroboration promotion, the appointment / EMR /
    geofence projections, the device binding — runs in `AnalysisPipeline.settle_tail`
    and nowhere else in a write path. So before this existed, a conversation-written
    appointment landed in NO projection and a conversation-written `name.*` fact never
    refreshed `canonical_name`: the graph held the fact, the appointments view did not.
    That gap was masked while the analyzer's settle retracted the conversation's facts
    and then projected the dead rows away; S1 made them survive, which is why S2 is the
    payment for S1's debt rather than an improvement on it
    (docs/plans/SETTLE_OWNERSHIP.md).

    **The release.** A `settle_owners` claim is released by a settle, and until S3 the
    conversation had none. So every row carrying a `conversation` claim — the rows only
    it wrote AND every row both producers assert — was retractable by no sweep at all,
    permanently, and the set grew with every co-asserted fact. Edit a note to drop a
    claim both producers wrote and the graph went on asserting it: a note no longer the
    sole source of truth for its own facts. `sweep_note` here is what stops that.

    **The sweep needs THREE more refusals than the state gate gives it, because the
    sweep is note-scoped and this producer is not a wholesale re-extractor.** The
    analyzer re-derives the whole note every pass, so its silence about a fact is a
    statement. This one asserts what is new and revises by supersession, so its silence
    is not — and it runs once per INGEST, in a fresh session, over a note earlier
    sessions of the same producer already wrote to. Each refusal below is a case where
    an empty or partial ledger would otherwise have been read as "the note no longer
    says that":

    - **the note moved under this thread.** `note_conversations.note_body_sha` is the
      text this pass read; if it no longer matches the note's composed body, the pass is
      judging a note that has changed since, and the conversation opened by that change
      is the one entitled to release. (`record_owner_reply` deliberately leaves the sha
      stale on a mismatch, so this is reachable without an edit racing the pass.)
    - **the pass held no graph-write verb.** On an `emr_owned` note, `narrow_for_emr`
      and the per-note registry leave the conversation unable to write anything
      (D9). Its empty ledger means "never asked", not "nothing to say" — and
      `emr_owned` reads note state that MUTATES, so a note whose PDF lands after the
      body was ingested flips from writable to not between two ordinary passes.
    - **an empty generation ledger.** With nothing asserted by any pass over this text,
      there is no re-derivation to compare against, so the release has no evidence
      behind it and would run on the whole note.

    All three fail toward a LEAK — a `conversation` claim nobody releases — which is the
    direction this design fails in deliberately (`analysis/settle_owner.py`). The tail
    still runs in every one of them: projecting is never destructive.

    **`state` is the gate, and it is the whole safety argument.** The pass settles only
    from `SETTLED`, which `state_for_stop` gives to a CLEAN stop alone — so a truncated
    turn (`max_steps`, the cost budget, consecutive tool errors, the wall clock) lands
    `failed`, a turn that ended on `ask_owner` lands `waiting_on_owner`, and a turn whose
    ledger did not record lands `failed` too, because both callers degrade the stop reason
    to `record_failed` when their recorder fails (`converse._run_turn`,
    `record_reply_writes` + `close_owner_reply` in `api/agent.py`). None of those three
    reaches this function's body. That is not a nicety: the sweep retracts every unpinned
    fact of the note the ledger does not vouch for, so firing it on an incomplete ledger
    retracts the owner's own writes — the bug S1 just closed, re-entered through the front
    door. An empty ledger under `settled` is a real statement ("this pass asserted
    nothing") and is swept on; an empty ledger under anything else is "nothing was
    recorded", and never reaches here.

    Never raises. A pass that settled is already `settled` in the database, and a failed
    settle leaks a claim — recoverable by the next settle of the note, and the direction
    this whole design fails in deliberately. Raising instead would retry the worker job,
    which re-enters `note_converse` for a note whose conversation is no longer live and
    opens a SECOND thread for it.
    """
    if state != SETTLED:
        return False
    retracted: set[uuid.UUID] = set()
    swept = False
    try:
        async with scoped_session(maker, ctx) as s:
            repo = NoteConversationRepo()
            conversation = await repo.get(s, session_id)
            if conversation is None:
                return False
            note_id = conversation.note_id
            # This pass's own writes drive the TAIL: what it touched is what wants
            # reprojecting. The sweep's `touched` is a different set entirely, below.
            entities = set((await repo.writes(s, session_id)).entities)
        note = await notes.get_note(ctx, str(note_id))
        refusal = _sweep_refusal(note, conversation.note_body_sha)
        async with scoped_session(maker, ctx) as s:
            if refusal is None:
                repo = NoteConversationRepo()
                # NOTE-scoped, not session-scoped, because the sweep is: every pass over
                # THIS TEXT is one derivation by one producer, and a per-session share
                # would release the claims every earlier session of the note laid down
                # (`NoteConversationRepo.writes_for_generation`).
                touched = set(
                    (
                        await repo.writes_for_generation(
                            s,
                            session_id=session_id,
                            note_id=note_id,
                            body_sha=conversation.note_body_sha,
                        )
                    ).facts
                )
                if touched:
                    # `mentions=None` SKIPS the mention reconcile rather than running it
                    # against an empty set. The ledger has no mention-id column, and an
                    # empty set would release this producer's claim on every mention of
                    # the note — including the spans the facts it still asserts are
                    # anchored to. `sweep_note` states what that leaks and why it is the
                    # bounded half.
                    retracted = await pipeline.sweep_note(
                        s,
                        note_id=note_id,
                        settle_owner=CONVERSATION,
                        touched=touched,
                        mentions=None,
                    )
                    swept = True
                else:
                    refusal = "empty_generation_ledger"
            # NOT `stamp_analysis`: the conversation has no title/tags verb, so it would
            # blank the analyzer's extracted title (SETTLE_OWNERSHIP.md precondition 3,
            # still unowned). NOT the `integration_state` flip either (precondition 4) —
            # a thread that can park on `ask_owner` for days cannot be what declares a
            # note integrated.
            await pipeline.settle_tail(s, referenced=entities, projected=entities | retracted)
    except Exception as exc:  # noqa: BLE001 — a leaked claim, never a retried job
        log.warning("note_settle.failed", session_id=session_id, error=repr(exc))
        return False
    log.info(
        "note_settle.done",
        session_id=session_id,
        entities=len(entities),
        swept=swept,
        retracted=len(retracted),
        refused=refusal,
    )
    return True


def _sweep_refusal(note: NoteInfo | None, read_sha: str) -> str | None:
    """Why this pass may not RELEASE anything, or None when it may — the three refusals
    `settle_conversation` documents, in the order they are cheapest to check.

    Fails closed: a note it cannot read refuses, because every one of these questions is
    about the note and an unanswerable one is not a licence."""
    if note is None:
        return "note_unreadable"
    if emr_owned(note.domain, note.destination, [a.media_type for a in note.attachments]):
        # The same predicate `converse.note_owned_by_emr` and `reply_profile_for_session`
        # narrow on, so "could this pass write?" has one answer across all three.
        return "no_write_verb"
    if note_body_sha(note.body) != read_sha:
        return "note_moved"
    return None


__all__ = [
    "NOTE_CONVERSE_AGENT",
    "SELF_RECORDED_TOOLS",
    "LedgerRow",
    "OwnerReply",
    "bind_turn_writes",
    "close_owner_reply",
    "ledger_rows",
    "record_owner_reply",
    "record_reply_writes",
    "record_turn_writes",
    "settle_conversation",
]
