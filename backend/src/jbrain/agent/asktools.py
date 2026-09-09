"""`ask_owner` — the note conversation's one question, and the turn ending on it.

W3/T2b of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md. D2 sets the posture (the agent
commits its reading and only asks when it genuinely cannot proceed) and TOOL_SURFACE.md
says the lever for that is description text, not schema — so the calibration lives in
`ask_owner.tool`, pointing at the bar the `note_ingest` prompt already sets rather than
restating it in different words.

What this module owns is the mechanics, and there are exactly three:

1. **The question becomes durable at ASK time**, in the same transaction as the state
   flip. W2 records the ledger AFTER the turn (`converse._record`), which is fine for an
   audit row and wrong for this one: the reply path reads the question back out of the
   ledger to compose the clarification block, so a question that exists only in a
   post-turn write is a question the owner can be waiting on while nothing knows what it
   was. This is the first piece of the recorder move W2 left open ("moving the recorder
   into the tool dispatch"); `converse.ledger_rows` skips what was self-recorded here so
   the two do not double up.

2. **The conversation goes to `waiting_on_owner`.** That state is the notes tab's query
   (D4/D5), it holds the note's one live slot so no second pass starts over a note whose
   reading is unfinished, and — constraint 6 — it is the state `NoteConversationRepo`
   refuses to move to `settled`, so the whole-note settle sweep can never run over a pass
   that stopped halfway to ask something.

3. **The turn ends.** Not by asking the model to stop — TOOL_SURFACE.md is explicit that
   gpt-oss does not honour protocol obligations stated in prose — but by the loop: the
   handler returns `ToolOutput(halt=...)` and `AgentLoop` finishes the turn on it without
   another model call, the same mechanism a deferred tool already uses. See `ask_owner.tool`
   for why that ordering is also stated to the model (write first, ask last): the halt
   enforces "no more steps", and the description is what stops the model wasting the ask.

A second `ask_owner` in the same turn is refused rather than recorded. Two open questions
on one conversation would leave the reply path guessing which one the owner's next
message answers, and the answer becomes source text on the note — a block that pairs an
answer with the wrong question is a wrong sentence in the corpus, not a cosmetic slip.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.toolregistry import ToolHandler
from jbrain.db.session import scoped_session
from jbrain.models.note_conversation import (
    AWAITING_OWNER,
    MAX_ARG_CHARS,
    InvalidStateTransition,
    NoteConversationRepo,
)

log = structlog.get_logger()

ASK_OWNER_TOOL = "ask_owner"

# `AWAITING_OWNER` (imported) is the loop STOP REASON this tool ends its turn with — a
# reason, not a state name, because that is what `AgentResult.stop_reason` means
# everywhere else. `models.note_conversation.state_for_stop` is the one place that turns
# it into the `waiting_on_owner` state, so the loop keeps saying why a turn stopped and
# the conversation repo keeps saying which transitions are legal.

# The model-facing cap on one question. Long enough for a real disambiguation ("Which
# Sarah — your sister, or Sarah Chen from work?"), short enough that a hostile note body
# cannot drive a 200 KB "question" into the ledger and, through the reply path, into the
# note's own text (plan risk 1). The ledger caps its `args` blob anyway; this cap is the
# one the OWNER sees, because this string is what the clarification block quotes back.
MAX_QUESTION_CHARS = MAX_ARG_CHARS


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


async def latest_question(
    session: AsyncSession, repo: NoteConversationRepo, session_id: str
) -> str | None:
    """The question this conversation is waiting on — the newest recorded `ask_owner`.

    Newest rather than "the one unanswered row" because the ledger has no answered flag
    and needs none: a conversation admits one open question at a time (a second ask is
    refused below), so the newest `ask_owner` on a `waiting_on_owner` thread IS the open
    one."""
    calls = await repo.tool_calls(session, session_id)
    for call in reversed(calls):
        if call.name == ASK_OWNER_TOOL and call.ok:
            question = _clean((call.args or {}).get("question"))
            if question:
                return question
    return None


def build_ask_owner_handlers(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, ToolHandler]:
    """The `ask_owner` handler, bound to the note-conversation tables.

    Every failure is returned as TEXT (loop.py turns a raised exception into a generic
    "hit an internal error" the model learns nothing from) — and every failure leaves the
    turn RUNNING, deliberately: a question that was not recorded must not stop the pass,
    or a note whose ask failed settles with its reading half-written and no question to
    show for it."""

    repo = NoteConversationRepo()

    async def ask_owner(arguments: Mapping[str, object], ctx: ToolContext) -> str:
        question = _clean(arguments.get("question"))[:MAX_QUESTION_CHARS]
        if not question:
            return (
                "ask_owner needs a `question`: one sentence naming the specific thing you"
                " cannot settle from the note. Nothing was recorded."
            )
        session_id = ctx.agent_session_id
        if session_id is None:
            return (
                "ask_owner works only inside a note's conversation, and this turn is not"
                " one. Nothing was recorded; answer from what you have."
            )
        async with scoped_session(maker, ctx.session) as s:
            conversation = await repo.get(s, session_id)
            if conversation is None:
                return (
                    "ask_owner works only inside a note's conversation, and this one has no"
                    " note behind it. Nothing was recorded; answer from what you have."
                )
            if conversation.state == "waiting_on_owner":
                open_question = await latest_question(s, repo, session_id) or "your last question"
                return (
                    "This note is already waiting on Jeff for: "
                    f"{open_question!r}. One open question at a time — this one was not"
                    " recorded."
                )
            # The ledger row and the state land together. The row is what the reply path
            # reads to build the clarification block, and the state is what tells the
            # owner (and the notes tab) there is something to answer: one without the
            # other is either a question nobody is asked or a wait nobody can explain.
            await repo.record_tool_call(
                s,
                session_id,
                name=ASK_OWNER_TOOL,
                args={"question": question},
                ok=True,
                # It wrote no graph. `domains` has no default precisely so a call that
                # touched no domain has to say so (0191).
                domains=(),
                detail=question,
            )
            try:
                await repo.set_state(s, session_id, "waiting_on_owner")
            except InvalidStateTransition:
                # `settled`/`failed` are terminal: a pass whose state already closed
                # cannot re-open to hold a question, and recording one would advertise a
                # wait the note is not in. Raising rolls the ledger row back with it.
                log.warning(
                    "ask_owner.conversation_closed",
                    session_id=session_id,
                    state=conversation.state,
                )
                raise
        log.info("ask_owner.recorded", session_id=session_id)
        return ToolOutput(
            "Recorded. This note is now waiting on Jeff, and your turn ends here — what"
            " you already wrote stands. When he answers, his answer is appended to the"
            " note and you pick the thread up from there.",
            halt=AWAITING_OWNER,
        )

    async def _guarded(arguments: Mapping[str, object], ctx: ToolContext) -> str:
        try:
            return await ask_owner(arguments, ctx)
        except Exception as exc:  # noqa: BLE001 — a failed ask is an observation, not a crash
            log.warning("ask_owner.failed", error=repr(exc))
            return (
                "ask_owner could not record the question, so the note is NOT waiting on"
                " anyone. Do not tell Jeff you asked him something. Carry on with what you"
                " can settle from the note itself."
            )

    return {ASK_OWNER_TOOL: _guarded}


TOOLS_DIR = Path(__file__).parent / "tools"
"""Where the `.tool` sidecars live. Spelled out again rather than imported from
`readtools`, which is the whole chat tool surface — importing it to learn a path would
pull blobs, search, the entity repos and the vision clients into the worker process,
which is exactly what the note conversation's by-name registry avoids. That registry is
`graphwritetools.note_registry`, built once per note in `analysis/converse.py` over the
handlers here plus the note-bound graph writes."""
