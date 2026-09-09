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
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.asktools import latest_question
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.note_conversation import (
    NoteConversationRepo,
    note_body_sha,
    state_for_stop,
)
from jbrain.notes.service import NotesRepo

log = structlog.get_logger()

NOTE_CONVERSE_AGENT = "note_ingest"
"""The persona whose sessions are note conversations. Spelled here rather than imported
from `analysis/converse.py`, which drags the whole turn runner (and through it the LLM
stack) into the API process for the sake of one string."""


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
) -> None:
    """End the conversation the owner's reply re-opened, by the same rule the unattended
    pass ends by (`state_for_stop`).

    Something has to: `record_owner_reply` put the thread back in `running`, and `running`
    holds the note's ONE live slot — the re-ingest the answer just queued emits its own
    `note.ingested`, and the pass that event opens is suppressed while this one stands. So
    a reply turn that never closed would leave the answered note un-re-read, until the
    stale-conversation reaper eventually called it `failed` an hour later.

    A turn the agent ended with another `ask_owner` is left exactly where the handler put
    it: the thread is waiting again, and `state_for_stop` says so."""
    if agent != NOTE_CONVERSE_AGENT:
        return
    state = state_for_stop(stop_reason)
    repo = NoteConversationRepo()
    try:
        async with scoped_session(maker, ctx) as s:
            conversation = await repo.get(s, session_id)
            if conversation is None or conversation.state != "running":
                return
            await repo.set_state(s, session_id, state)
    except Exception as exc:  # noqa: BLE001 — the reaper is the backstop
        log.warning("note_reply.close_failed", session_id=session_id, error=repr(exc))
        return
    log.info("note_reply.closed", session_id=session_id, state=state, stop_reason=stop_reason)


__all__ = [
    "NOTE_CONVERSE_AGENT",
    "OwnerReply",
    "close_owner_reply",
    "record_owner_reply",
]
