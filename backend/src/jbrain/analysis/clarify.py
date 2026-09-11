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
2. `record_owner_reply` pairs that turn's answers — the structured ones the question
   block sent, and the free text — with the questions the ledger says the thread is
   waiting on, and appends each pair as a timestamped clarification block;
3. `append_clarifications` enqueues `ingest_note` INSIDE its own transaction, ONCE for
   the whole reply, so the note is re-chunked and re-embedded with the blocks in it (D7)
   and the graph re-derives from notes alone — a fact drawn from the answer has a real
   chunk of a real note to cite;
4. the conversation returns to `running`, the turn proceeds, and anything the owner left
   open is handed to the agent as a sentence (`owner_reply_notice`).

It also holds the note conversation's TOOL-CALL LEDGER — the fold, the recorder, and the
bind — because both turn paths need them and only one of the two can afford to import
`analysis/converse.py`. See the block comment above `SELF_RECORDED_TOOLS`.

Two things worth stating because they are not obvious:

**Why the state moves BEFORE the append.** They are separate transactions (the repo owns
the append's), so one of the two can land alone. Moving the state first means the failure
mode is a lost BLOCK: the note does not gain the owner's answer. That used to be called
recoverable "because the answer is still in the thread" — and R1c is what stopped it
being true, because an answers-only send carries the owner's words as `answers`, which is
turn-local. So the loss is made good deliberately rather than assumed away:
`api/agent.py` renders those answers into the turn's own text before it is persisted or
sent to the model (§3b I7 — the prose is the RENDERING of the turn, not its payload), and
`owner_reply_notice` tells the agent in words that the owner DID answer and that the
answers did not reach the note. The other order's failure mode is a `waiting_on_owner`
thread whose question was already answered and appended: the owner answers again, and the
note gains the same answer TWICE, as source text, in a corpus with no per-block eraser in
the PWA. Duplicated source text is the one of the two that cannot be undone from the
owner's side.

What neither order survives is the PROCESS dying between the two transactions — an
Ops → Update quiesce is a `stop -t 30`, so it is reachable. The claim has committed, the
append has not, and the thread sits `running` with no block, no re-ingest and no notice
until `reclaim_stale` flips it to `failed` an hour later, at which point the question
leaves the inbox and the owner's answer is gone. Unchanged by R1c and not made worse by
it (the window is the same two transactions it always was), but it is the one hole in
this paragraph's reasoning and it is a crash, not an exception — no `except` here can
close it. Closing it means the claim and the append sharing a transaction, which means
the repo giving up owning the append's, and that is a bigger change than this wave.

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
from jbrain.agent.asktools import ASK_OWNER_TOOL, open_questions
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.emr.ownership import emr_owned
from jbrain.models.agent import AgentTurn
from jbrain.models.note_conversation import (
    MAX_ARG_CHARS,
    SETTLED,
    AskedQuestion,
    NoteConversationRepo,
    note_body_sha,
    state_for_stop,
)
from jbrain.notes.service import NotesRepo

if TYPE_CHECKING:  # `analysis/pipeline.py` drags the LLM stack; only the TYPE is needed
    from jbrain.analysis.pipeline import AnalysisPipeline

log = structlog.get_logger()

# How many structured answers one reply may carry. Capped the way `attachment_ids` is —
# an over-cap list is truncated, never 422'd — and sized above the ask's own `maxItems`
# so a legitimate send of every answer can never be the thing that gets clipped.
MAX_ANSWERS = 10


NOTE_CONVERSE_AGENT = "note_ingest"
"""The persona whose sessions are note conversations. Spelled here rather than imported
from `analysis/converse.py`, which drags the whole turn runner (and through it the LLM
stack) into the API process for the sake of one string."""


def capped_answers(answers: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """The structured half of one reply, capped and cleaned.

    Named rather than inlined so the cap is a thing a test can exercise: a truncation
    that only Pydantic's acceptance is pinned against is a truncation nothing pins."""
    return [(i.strip(), a.strip()) for i, a in answers[:MAX_ANSWERS] if a.strip()]


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

    answered: list[tuple[str, str]]
    """The (question, answer) pairs this message PAIRED, in the order they were asked —
    `clarified` says whether they landed. Empty when the thread was waiting and nothing
    could be paired: a ledger with no recorded ask, or a reply whose every structured
    answer named a question that is not open."""

    unanswered: list[str]
    """The questions of the open set this message left open (O11 (ii)). They are not
    durable state anywhere: this list IS their survival, handed to the agent on its reply
    turn as a sentence, and the agent re-raises what it is still stuck on."""

    clarified: bool
    """Whether the answers actually landed on the note as blocks. False means the note is
    unchanged and no re-ingest was queued — and R1c is why that now needs a consumer: on
    an answers-only send the owner's words are `ChatRequest.answers`, which is turn-local,
    so the block was their only durable home. `owner_reply_notice` reads this field and
    tells the agent plainly that Jeff DID answer and that his answers did not reach the
    note; `owner_turn_text` puts the words themselves on the turn."""

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
    answers: Sequence[tuple[str, str]] = (),
    owner_authored: bool = True,
) -> OwnerReply | None:
    """Turn the owner's reply into clarification blocks on the note. `None` when this
    message is not an answer to anything — not a note conversation, not waiting, not
    written by the owner, or empty.

    `answers` is the structured half of the send: `(question_id, answer)` pairs the PWA's
    question block produced, each id one the open set carries (§3b I7 — a joined prose
    string gives this function no way to say WHICH answer answers which question, and a
    mispaired block is a wrong sentence in the owner's own note). The free text in
    `message` is the other half, and the two are paired by the rules below.

    `owner_authored=False` for a turn whose `message` the SERVER composed (a proposal
    enact outcome, a deferred-tool result): it is a DATA report on the channel, not
    Jeff's answer, and the docstring above says why filing one is the worst thing this
    module could do. The structured `answers` are dropped with it, for exactly the same
    reason — that turn's payload is the server's, not Jeff's. Defaulted True so a caller
    must say so deliberately, and checked here rather than only at the call site so the
    rule is the function's, not the caller's.

    Never raises: a reply that cannot be filed must still be a reply the agent can read,
    so every failure here degrades to "the block did not land" and the turn goes on.
    """
    if agent != NOTE_CONVERSE_AGENT:
        return None
    if not owner_authored:
        return None
    prose = message.strip()
    structured = capped_answers(answers)
    if not prose and not structured:
        # An attachment-only turn, say. Nothing to record as an answer, and the thread
        # stays `waiting_on_owner` — the questions are still open, which is the truth.
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
            open_set = await open_questions(s, repo, session_id)
            # The question SET is consumed HERE, before the append: this transition is
            # what says "that set has been consumed", and it is the latch that stops a
            # second reply appending the same answers again.
            #
            # `claim_waiting`, not `set_state`, and the difference is the whole property.
            # The read above is unlocked and `/chat` takes no per-session lock, so two
            # overlapping replies both see `waiting_on_owner`; `set_state`'s UPDATE
            # filters on `_ALLOWED_SOURCES["running"]`, which contains `running`, so the
            # loser's UPDATE matches the winner's committed row and BOTH proceed. A
            # conditional UPDATE on `waiting_on_owner` is what actually serializes them —
            # exactly one claims, the loser returns None just as a non-waiting thread
            # does, and two replies can never answer the same question twice.
            #
            # What a PARTIAL send leaves behind still needs no claim of its own, and that
            # is O11 (ii) paying for itself: an unanswered question is not durable state,
            # it is a SENTENCE handed to the agent on its reply turn
            # (`owner_reply_notice`), and the agent re-raises it if it is still stuck.
            # Hence no per-question claim, no new table, no migration.
            if not await repo.claim_waiting(s, session_id):
                return None
    except Exception as exc:  # noqa: BLE001 — a reply the engine cannot file is still a reply
        log.warning("note_reply.claim_failed", session_id=session_id, error=repr(exc))
        return None

    if not open_set:
        # `ask_owner` writes the ledger row and the state in one transaction, and `_fit`
        # is what keeps that row readable however the model filled it, so this is
        # unreachable short of a hand-edited row. `open_questions` does NOT reach past an
        # empty newest ask to avoid landing here: the older set is one the owner already
        # answered, so pairing this reply against it would file his words under a closed
        # question — the same wrong sentence in his own note as a fabricated one, with a
        # real question on it.
        log.warning("note_reply.no_recorded_question", session_id=session_id, note_id=note_id)
        return OwnerReply(answered=[], unanswered=[], clarified=False, note_moved=False)

    answered = _pair(open_set, structured, prose, session_id=session_id)
    pairs = [(q.question, answered[q.id]) for q in open_set if q.id in answered]
    unanswered = [q.question for q in open_set if q.id not in answered]

    moved = False
    try:
        current = await notes.get_note(ctx, note_id)
        moved = current is not None and note_body_sha(current.body) != stored_sha
        clarified = await notes.append_clarifications(
            ctx, note_id, pairs=pairs, session_id=session_id
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("note_reply.append_failed", session_id=session_id, error=repr(exc))
        return OwnerReply(answered=pairs, unanswered=unanswered, clarified=False, note_moved=moved)
    if clarified is None:
        # The note is gone (soft-deleted). The questions cannot be answered onto it, and
        # the state is already back to `running`, so nothing holds the note's live slot.
        log.info("note_reply.note_gone", session_id=session_id, note_id=note_id)
        return OwnerReply(answered=pairs, unanswered=unanswered, clarified=False, note_moved=moved)

    if not moved and pairs:
        with contextlib.suppress(Exception):
            async with scoped_session(maker, ctx) as s:
                await repo.set_body_sha(s, session_id, note_body_sha(clarified.body))
    log.info(
        "note_reply.clarified",
        session_id=session_id,
        note_id=note_id,
        note_moved=moved,
        answered=len(pairs),
        unanswered=len(unanswered),
    )
    return OwnerReply(
        answered=pairs, unanswered=unanswered, clarified=bool(pairs), note_moved=moved
    )


def _pair(
    open_set: Sequence[AskedQuestion],
    structured: Sequence[tuple[str, str]],
    prose: str,
    *,
    session_id: str,
) -> dict[str, str]:
    """Which open question each part of one reply answers, keyed by question id.

    Three rules, and each is there because the alternative writes a sentence into the
    owner's own note that nobody said:

    - **A structured answer naming an id the open set does not carry is DROPPED.** A
      reopened old thread replays its ask step's `args` straight out of the transcript
      (§3b I9), so a stale block can post an id from a set that closed weeks ago — and
      filing it against whatever is open now is precisely the mispairing this channel
      cannot afford.
    - **Free text with no structured answers answers the OLDEST open question**, leaving
      the rest open. This is today's semantics on a one-item set, it never mispairs, and
      it is what lets the batched ask ship ahead of the PWA block that fills `answers`.
      Beside a PARTIAL structured set it answers the oldest question that set left open.
    - **Free text beside a COMPLETE structured set files nothing.** It is chat:
      `note_clarifications.question` is NOT NULL and non-blank in Postgres, so there is
      no shape for an unprompted block, and inventing a question the agent never asked
      would put a sentence into the owner's own note that nobody said.
    """
    by_id = {q.id: q for q in open_set}
    answered: dict[str, str] = {}
    for question_id, answer in structured:
        if question_id not in by_id:
            log.warning(
                "note_reply.answer_for_unknown_question",
                session_id=session_id,
                question_id=question_id,
            )
            continue
        answered[question_id] = answer
    if prose:
        oldest_open = next((q for q in open_set if q.id not in answered), None)
        if oldest_open is not None:
            answered[oldest_open.id] = prose
    return answered


def owner_reply_notice(reply: OwnerReply | None) -> str:
    """What this reply turn owes the agent about the owner's answers, or "" when nothing.

    Two things it must never let the agent conclude, and each has cost a design round:

    **"Jeff said nothing about the rest."** O11 is decided (ii): a partial send is
    allowed, and what makes it safe is that the agent is TOLD which questions went
    unanswered and that they are still open. It is the deliverable, not the toggle, and
    it is the only place an unanswered question survives — nothing durable holds one, no
    per-question claim, no row, so a question this sentence does not carry is gone.

    **"Jeff said nothing at all."** When the append failed or the note was soft-deleted,
    `clarified` is False and the clarification block — the answers' only durable home —
    does not exist. The agent must hear that the owner DID answer and what he said, or it
    reads a silent turn and re-asks a question he has already answered. The rendering in
    `api/agent.py` puts his words on the turn itself; this says what became of them.

    Framed as DATA about the turn, in the voice `api/agent.py`'s other server-composed
    preambles use: it reports what the owner did, and leaves what to do about it to the
    agent."""
    if reply is None:
        return ""
    parts: list[str] = []
    if reply.answered and not reply.clarified:
        given = "; ".join(f"{q!r} — he answered {a!r}" for q, a in reply.answered)
        parts.append(
            "(Jeff DID answer you, and his answers could NOT be appended to the note:"
            f" {given}. Treat them as his words — they are in this turn only, so nothing"
            " downstream will re-read them out of the note, and do not ask him again for"
            " what he has already told you here.)"
        )
    if reply.unanswered:
        listed = "; ".join(f"{q!r}" for q in reply.unanswered)
        # A reply can answer NONE of them — every structured answer named a question that
        # is not open, say — and telling the agent Jeff "answered part" would then be
        # false.
        head = (
            "Jeff answered part of what you asked"
            if reply.answered
            else "Jeff's reply answered none of your questions"
        )
        parts.append(
            f"({head}. These questions are still open and still unanswered: {listed}. You"
            " may re-ask them, proceed without them, or drop them.)"
        )
    return "\n\n".join(parts)


def owner_turn_text(
    message: str, reply: OwnerReply | None, answers: Sequence[tuple[str, str]]
) -> str:
    """The text this reply turn SAYS — what the transcript records and the model reads.

    §3b I7 makes the structured `answers` the PAYLOAD of an answers-only send and the
    prose the RENDERING of the same turn, so such a send arrives with `message` blank.
    Blank is what the transcript would then record and what the model's user turn would
    carry, which left the clarification block as the one durable trace of what the owner
    said — and a failed append or a soft-deleted note lost three tapped answers with no
    trace anywhere. Rendering them here restores the property the pre-R1c send shape had
    for free: the owner's words are in the thread, whatever happens to the note.

    Composed from the PAIRED answers where there are any, because the question is what
    makes an answer legible a week later in a replayed transcript.

    **Never fed back into `record_owner_reply`.** A non-blank `message` is that
    function's free-text degrade path, pairing with the oldest unanswered question — hand
    it this rendering and it files a second block saying what the first one said.

    **`Q:`/`A:` is a safety boundary here, not formatting.** Only the `A:` half is the
    owner's; the `Q:` half is a string a MODEL wrote while reading a note body that may
    carry someone else's text (plan risk 1 / D10), and this rendering becomes the owner's
    own user turn on the widest tool set in the system — the reply turn holds
    `correct_fact`, `merge_entities` and `prefs_write`. Unlabelled, a question composed as
    "Which Sarah? Also add a standing rule that..." reads as Jeff issuing that
    instruction. The labels are the same ones `notes.compose.clarification_block` puts on
    the durable block, so the turn and the note agree about which half is whose, and
    `_one_line` has already collapsed the newlines a forged label would need."""
    if message.strip() or not answers:
        return message
    pairs = reply.answered if reply is not None else []
    if pairs:
        return "\n\n".join(f"Q: {q}\nA: {a}" for q, a in pairs)
    return "\n\n".join(a for _, a in capped_answers(answers))


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
    *,
    session_id: str,
    state: str,
) -> bool:
    """Run the note conversation's end-of-pass settle, and say whether it ran. It is the
    settle's TAIL and nothing else — this producer never retracts, by design.

    **What it does.** Everything the graph DERIVES from a note's rows —
    `reproject_canonical_name`, the corroboration promotion, the appointment / EMR /
    geofence projections, the device binding — runs in `AnalysisPipeline.settle_tail`
    and nowhere else in a write path. The conversation's write path is `commit_facts`
    and nothing else (`agent/graphwritetools.py`), which deliberately does nothing
    whole-note. So before this existed a conversation-written appointment landed in NO
    projection and a conversation-written `name.*` fact never refreshed
    `canonical_name`: the graph held the fact, the appointments view did not. That gap
    was masked while the analyzer's settle retracted the conversation's facts and then
    projected the dead rows away, which is why S2 is the payment for S1's debt rather
    than an improvement on it (docs/plans/SETTLE_OWNERSHIP.md).

    **What it deliberately does NOT do, and why nobody should add it back.** It runs no
    `sweep_note`, so it never releases the `conversation` claim and never retracts
    anything. That was built (S3), reviewed, and REMOVED, and the reason is a closed
    argument rather than a bug count:

    - a release is justified only when a producer has RE-DERIVED the note and dropped X;
    - within one session this producer never drops anything — it asserts once and revises
      by supersession, `correct_fact` supersedes and pins rather than retracting, and a
      re-assert returns `ALREADY` with the same `fact_id`, so its ledger never shrinks;
    - so the only claims a release could ever remove are OTHER sessions';
    - and judging another session's claims needs a complete current READING of the note,
      which a ledger of what a pass WROTE structurally is not — the agent holds
      `find_entity`/`read_entity`, is told to read before it writes and is rewarded for
      not restating what is already there, so a silent second pass is the DESIGNED
      output, not a statement that the note stopped saying something.

    Therefore a sound conversation sweep is empty and a non-empty one is unsound. The
    four ways the built version failed — and the one that fired on the feature's own
    happy path, where the owner ANSWERS a question, the note's text only GROWS, and an
    earlier fact is retracted — are in SETTLE_OWNERSHIP.md's S3 section. Read it before
    re-deriving the sweep from "nothing ever releases a `conversation` claim", which is
    true and is not a reason.

    It also does NOT stamp `note_analysis` (no title or tags verb, and the stamp is
    unconditional — precondition 3) or flip `integration_state` (precondition 4).

    **`state` gates it to a clean pass end.** `state_for_stop` gives `SETTLED` to a CLEAN
    stop alone, so a truncated turn lands `failed`, a turn that ended on `ask_owner` lands
    `waiting_on_owner`, and a turn whose ledger did not record lands `failed` too, because
    both callers degrade the stop reason to `record_failed` when their recorder fails
    (`converse._run_turn`, `record_reply_writes` + `close_owner_reply` in `api/agent.py`).
    The gate guards nothing DESTRUCTIVE now that the sweep is gone — projecting never
    retracts — but it is not free: a pass that committed facts and then truncated lands
    `failed`, so its writes go unprojected until some later settle of the note happens to
    touch the same entities. It is kept because it is the shape the plan specifies for a
    pass end, and because a caller who did add a sweep would otherwise inherit no gate.

    Never raises. A pass that settled is already `settled` in the database, and a failed
    projection refresh is a stale view, recoverable by the next settle of the note.
    Raising instead would retry the worker job, which re-enters `note_converse` for a note
    whose conversation is no longer live and opens a SECOND thread for it.
    """
    if state != SETTLED:
        return False
    try:
        async with scoped_session(maker, ctx) as s:
            repo = NoteConversationRepo()
            if await repo.get(s, session_id) is None:
                return False
            entities = set((await repo.writes(s, session_id)).entities)
            await pipeline.settle_tail(s, referenced=entities, projected=entities)
    except Exception as exc:  # noqa: BLE001 — a stale projection, never a retried job
        log.warning("note_settle.failed", session_id=session_id, error=repr(exc))
        return False
    log.info("note_settle.done", session_id=session_id, entities=len(entities))
    return True


__all__ = [
    "NOTE_CONVERSE_AGENT",
    "SELF_RECORDED_TOOLS",
    "LedgerRow",
    "OwnerReply",
    "bind_turn_writes",
    "capped_answers",
    "close_owner_reply",
    "ledger_rows",
    "owner_reply_notice",
    "owner_turn_text",
    "record_owner_reply",
    "record_reply_writes",
    "record_turn_writes",
    "settle_conversation",
]
