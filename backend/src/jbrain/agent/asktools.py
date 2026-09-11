"""`ask_owner` — the note conversation's open question SET, and the turn ending on it.

W3/T2b of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, batched by R1c of
docs/plans/AGENT_INGEST_REWRITE.md. D2 sets the posture (the agent commits its reading
and only asks when it genuinely cannot proceed) and TOOL_SURFACE.md says the lever for
that is description text, not schema — so the calibration lives in `ask_owner.tool`,
pointing at the bar the `note_ingest` prompt already sets rather than restating it in
different words.

What this module owns is the mechanics, and there are exactly three:

1. **The questions become durable at ASK time**, in the same transaction as the state
   flip. W2 records the ledger AFTER the turn (`converse._record`), which is fine for an
   audit row and wrong for this one: the reply path reads the questions back out of the
   ledger to compose the clarification blocks, so a question that exists only in a
   post-turn write is a question the owner can be waiting on while nothing knows what it
   was. It is the one tool that records inside its own transaction rather than at the
   turn seam every other call is recorded at (`clarify.record_turn_writes`, which both
   turn paths share); `clarify.ledger_rows` skips `SELF_RECORDED_TOOLS` so the two do not
   double up — including on a REPLY turn that ends by asking again. Each question carries
   an id assigned here, and it is stored in the `args` blob because that blob is the only
   thing that persists it: the PWA replays a settled thread's ask step out of the
   transcript (§3b I9), so an answer can only find its question through an id the args
   carry.

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

A second `ask_owner` in the same turn is refused rather than recorded, and the reason is
now about the SET rather than about arity. One reply consumes one open set
(`clarify.record_owner_reply` flips `waiting_on_owner -> running` before it appends), so
a second set opened behind the first would be answered by nothing and would leave two
"open" sets the reply path has no rule to choose between. The first ask already ended the
turn; everything the pass is stuck on belongs in it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.toolregistry import ToolHandler
from jbrain.db.session import scoped_session
from jbrain.models.note_conversation import (
    AWAITING_OWNER,
    MAX_ARG_CHARS,
    MAX_ARGS_CHARS,
    AskedQuestion,
    InvalidStateTransition,
    NoteConversationRepo,
    questions_from_args,
)

log = structlog.get_logger()

ASK_OWNER_TOOL = "ask_owner"

# `AWAITING_OWNER` (imported) is the loop STOP REASON this tool ends its turn with — a
# reason, not a state name, because that is what `AgentResult.stop_reason` means
# everywhere else. `models.note_conversation.state_for_stop` is the one place that turns
# it into the `waiting_on_owner` state, so the loop keeps saying why a turn stopped and
# the conversation repo keeps saying which transitions are legal.

# How many questions one ask may carry, matching the sidecar's `maxItems`. Clamped here
# too because a schema bound is a request, not an enforcement.
MAX_QUESTIONS = 5

# A COARSE first cut on one FIELD of one question, matching the ledger's own per-string
# cap. Long enough for a real disambiguation ("Which Sarah — your sister, or Sarah Chen
# from work?") with its candidates, short enough that a hostile note body cannot drive a
# 200 KB "question" into the measurement below (plan risk 1). It is not the guarantee.
MAX_QUESTION_CHARS = MAX_ARG_CHARS

# What the recorded blob may SERIALIZE to, with margin under the total `cap_tool_args`
# enforces. Counted, never estimated, and that is the whole point: the cap is
# `len(json.dumps(blob))` and `json.dumps` defaults to `ensure_ascii=True`, so one source
# character is up to six serialized ones (`\uXXXX`, twelve for an astral pair) and a
# budget counted in SOURCE characters clears a Japanese question set by a factor of two.
# Over the cap the blob degrades to `{"_keys": [...]}`, which is the worst outcome this
# subsystem has: `questions_from_args` reads [] off it, so the thread waits on a question
# nothing can show, and the owner's answer then takes `record_owner_reply`'s empty-set
# branch — question and answer both gone, with no trace and nothing told. The margin
# covers the `_truncated` key `cap_tool_args` may add beside the measured content.
ASK_ARGS_BUDGET = MAX_ARGS_CHARS - 512


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def recorded_args(asked: Sequence[AskedQuestion]) -> dict[str, Any]:
    """Exactly the `args` blob `record_tool_call` is handed, so what `_fit` measures and
    what the ledger stores cannot drift apart."""
    return {"questions": [asdict(q) for q in asked]}


def _serialized(asked: Sequence[AskedQuestion]) -> int:
    return len(json.dumps(recorded_args(asked)))


def _clip_candidates(q: AskedQuestion, n: int) -> AskedQuestion:
    return replace(q, candidates=q.candidates[:n])


def _clip_blocks(q: AskedQuestion, n: int) -> AskedQuestion:
    return replace(q, blocks=q.blocks[:n])


def _clip_question(q: AskedQuestion, n: int) -> AskedQuestion:
    return replace(q, question=q.question[:n])


def _shrink(
    asked: list[AskedQuestion],
    clip: Callable[[AskedQuestion, int], AskedQuestion],
    floor: int,
) -> list[AskedQuestion]:
    """The longest UNIFORM per-item limit on one field that still fits, by bisection.

    Serialized size is monotonic in the limit, which is what makes bisecting sound;
    `MAX_QUESTION_CHARS` is the ceiling every field was already cut to, which is what
    bounds the loop. `floor` is the shortest the field may become — 0 for context, 1 for
    the question, since `questions_from_args` drops a BLANK question and dropping is the
    one thing this must not do."""
    best = [clip(q, floor) for q in asked]
    lo, hi = floor, MAX_QUESTION_CHARS
    while lo <= hi:
        mid = (lo + hi) // 2
        trial = [clip(q, mid) for q in asked]
        if _serialized(trial) <= ASK_ARGS_BUDGET:
            best, lo = trial, mid + 1
        else:
            hi = mid - 1
    return best


def _fit(asked: list[AskedQuestion]) -> list[AskedQuestion]:
    """Shrink the set until the blob it will be RECORDED as fits `ASK_ARGS_BUDGET`.

    Context first and the question last: `candidates` and `blocks` help the owner answer
    in one tap, the question is what he has to be able to read at all. And no question is
    ever dropped — a dropped one is a question the thread waits on that nothing records,
    which is the failure the budget exists to prevent, arrived at by another road."""
    for clip, floor in ((_clip_candidates, 0), (_clip_blocks, 0), (_clip_question, 1)):
        if _serialized(asked) <= ASK_ARGS_BUDGET:
            break
        asked = _shrink(asked, clip, floor)
    return asked


async def open_questions(
    session: AsyncSession, repo: NoteConversationRepo, session_id: str
) -> list[AskedQuestion]:
    """The question set this conversation is waiting on — the newest recorded `ask_owner`.

    Newest rather than "the rows with no answer" because the ledger has no answered flag
    and needs none: a conversation admits one open SET at a time (a second ask is refused
    below, and one reply consumes the whole set), so the newest `ask_owner` on a
    `waiting_on_owner` thread IS the open one.

    Whatever that row parses to is the answer, **empty included**. Falling through an
    empty newest row to an older one reads the invariant backwards: the older set is one
    the owner already answered, and returning it pairs this reply's words to a question
    that closed — the mispairing `clarify._pair` exists to prevent, arriving through the
    reader instead of the pairer. `_fit` is what makes an empty parse unreachable rather
    than merely wrong; were it to happen anyway, the reply files nothing and says so,
    which is a branch this path already has."""
    calls = await repo.tool_calls(session, session_id)
    newest = next((c for c in reversed(calls) if c.name == ASK_OWNER_TOOL and c.ok), None)
    return questions_from_args(newest.args) if newest is not None else []


def _asked(arguments: Mapping[str, object]) -> list[AskedQuestion]:
    """The set as this call states it, cleaned, capped, and given its ids.

    A blank `question` is dropped rather than refused: `required` buys PRESENCE, not
    membership (the measured rule that outlived the wave that found it), so an item with
    an empty string in it is a shape this handler has to survive. An item sent as a bare
    string rather than an object is read as its question for the same reason.

    `_fit` has the last word: the per-field cut below is coarse, and what the ledger will
    accept is measured on the way out."""
    raw = arguments.get("questions")
    asked: list[AskedQuestion] = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_QUESTIONS]:
        fields: Mapping[str, object] = item if isinstance(item, Mapping) else {"question": item}
        question = _clean(fields.get("question"))[:MAX_QUESTION_CHARS]
        if not question:
            continue
        asked.append(
            AskedQuestion(
                # Random rather than positional: a REPLY turn asks again on the same
                # conversation, and an answer is only ever matched against the OPEN set,
                # so an id that repeated across sets would let a stale block from a
                # reopened thread file against a question nobody asked.
                id=f"q{uuid.uuid4().hex[:8]}",
                question=question,
                blocks=_clean(fields.get("blocks"))[:MAX_QUESTION_CHARS],
                candidates=_clean(fields.get("candidates"))[:MAX_QUESTION_CHARS],
            )
        )
    return _fit(asked)


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
        asked = _asked(arguments)
        if not asked:
            return (
                "ask_owner needs `questions`: one or more items, each with a `question`"
                " naming a specific thing you cannot settle from the note. Nothing was"
                " recorded."
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
                return _already_waiting(await open_questions(s, repo, session_id))
            # The ledger row and the state land together. The row is what the reply path
            # reads to build the clarification blocks, and the state is what tells the
            # owner (and the notes tab) there is something to answer: one without the
            # other is either a question nobody is asked or a wait nobody can explain.
            await repo.record_tool_call(
                s,
                session_id,
                name=ASK_OWNER_TOOL,
                args=recorded_args(asked),
                ok=True,
                # It wrote no graph. `domains` has no default precisely so a call that
                # touched no domain has to say so (0191).
                domains=(),
                # The first question, not the set: `detail` is a one-line label for the
                # D3 chip, and the whole set is already in `args` for anything that needs
                # to read it back.
                detail=asked[0].question,
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
        log.info("ask_owner.recorded", session_id=session_id, questions=len(asked))
        return ToolOutput(
            f"Recorded, {_count(len(asked))}. This note is now waiting on Jeff, and your"
            " turn ends here — what you already wrote stands. When he answers, each"
            " answer is appended to the note and you pick the thread up from there.",
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


def _count(n: int) -> str:
    return "1 question" if n == 1 else f"{n} questions"


def _already_waiting(open_set: list[AskedQuestion]) -> str:
    """The refusal, at the level the latch works at: one open SET, not one question."""
    if not open_set:
        return (
            "This note is already waiting on Jeff for your last question. One open set at"
            " a time — this ask was not recorded."
        )
    return (
        f"This note is already waiting on Jeff for {_count(len(open_set))}, starting with:"
        f" {open_set[0].question!r}. One open set at a time — this ask was not recorded."
    )


TOOLS_DIR = Path(__file__).parent / "tools"
"""Where the `.tool` sidecars live. Spelled out again rather than imported from
`readtools`, which is the whole chat tool surface — importing it to learn a path would
pull blobs, search, the entity repos and the vision clients into the worker process,
which is exactly what the note conversation's by-name registry avoids. That registry is
`graphwritetools.note_registry`, built once per note in `analysis/converse.py` over the
handlers here plus the note-bound graph writes."""
