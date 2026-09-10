"""`note_converse` — the note read by the agent, in a visible thread.

W2/T4 of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, and the wave's honest retreat
point: *the agent reads a note in a visible thread*. No tools, no chip, no inbox, no
routes. The whole point of the wave landing is that the thread exists and can be
looked at.

It runs BESIDE `integrate_note`, never instead of it (D13: no PR removes a producer
before its replacement is merged). `note.ingested` therefore drives two pipelines —
the shipped integration that still writes the graph, and this conversation, which in
this wave writes nothing at all. That is plan risk 4, accepted at ratification.

What that costs, stated as it actually bills. It is one `agent.turn` per
`note.ingested` EVENT, and that is NOT one per note — the event fires on every SETTLED
ingest of a note, so a note re-ingested is a note charged again, in its own second
thread. Three re-ingests are shipped and ordinary:

- an attachment landing on an ALREADY-ingested note. Capture posts the note and its
  files as separate requests; the `attachments_expected` hint (`ingest/pipeline.py`)
  defers the first emit so a photo captured WITH its hint pays once, but a file added
  later — or any client that sends no hint — emits on the body, then again after OCR
  re-ingests. Two threads for one note;
- every D6 clarification: `append_clarification` enqueues `ingest_note` in its own
  transaction, so each answered question re-ingests and pays for another turn;
- a re-ingest for any other reason (an edited body).

`graph_rebuild` does NOT amplify it: `backfill_pending_integration` enqueues
`integrate_note` jobs directly against `app.jobs` and emits no event, so a corpus-wide
rebuild costs nothing here — which is the one thing that would otherwise multiply this
by the size of the corpus. W2 has no path that closes, merges or supersedes the threads
a re-ingested note accumulates; the notes tab that would show them is W3 (D4).

Three things make it an ordinary agent conversation rather than a second hidden ingest
path (D1):

- it opens a real `app.agent_sessions` row under the `note_ingest` persona, so it
  replays through the shipped `GET /sessions/{id}/transcript` and is listed by the
  shipped chat list — the PWA's Full Brain tab carries `note_ingest` alongside the
  curator (`frontend/src/agent/useFullBrain.ts`), which is what makes "the agent reads
  a note in a VISIBLE thread" true without a debug token. Listed, not landed on: the
  tab still opens the curator, and the notes tab that will point at these is W3 (D4);
- it runs its turn through the shared headless engine (`tasks/runner.LoopTurnExecutor`),
  the same one /chat and the task runner drive, rather than a bespoke loop;
- it persists through `AgentTranscript`, so the thread replays like any other.

What it does NOT inherit from an ordinary conversation is trust in its first turn.
Turn 0 is a note body, and a note body may be third-party text — an email, a stranger's
message, a page read off a photo (plan risk 1). `readtools.read_note` hands bodies to
the model unframed, which is safe today only because the persona reading them holds no
tools. This one will hold graph writes in W3, so the frame goes in NOW, while the cost
of getting it wrong is zero. It is `intake/turn.py`'s `_RECIPIENT_FRAME` pattern — the
per-turn half of the boundary, paired with the standing rule the `note_ingest` prompt
carries — with one difference: the frame here is CLOSED by a per-turn nonce. That
persona holds no tools in any wave, so an open-ended prefix is enough for it; this one
is the seat the graph writes go in, and an unterminated frame is impersonable by the
very text it fences.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent import readtools
from jbrain.agent.agents import (
    AgentProfile,
    agent_for,
    narrow_for_emr,
    narrow_for_third_party_note,
)
from jbrain.agent.asktools import build_ask_owner_handlers
from jbrain.agent.clock import build_clock_handlers, now_block
from jbrain.agent.graphwritetools import (
    NoteGraphWriter,
    NoteTarget,
    NoteToolset,
    note_registry,
)
from jbrain.agent.prefstools import with_standing_instructions
from jbrain.agent.readtools import build_entity_handlers
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.analysis.clarify import bind_turn_writes, record_turn_writes, settle_conversation
from jbrain.analysis.noteframe import OWN_NOTE_ABOUT, THIRD_PARTY_ABOUT, framed_note
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.analysis.thirdparty import is_third_party
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.emr.ownership import emr_owned
from jbrain.llm import LlmRouter, UserMessage
from jbrain.models.note_conversation import (
    NOTE_TURN_WALL_CLOCK,
    NoteConversationRepo,
    note_body_sha,
    state_for_stop,
)
from jbrain.models.owner_prefs import OwnerPrefsRepo
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notes.service import NoteInfo, NotesRepo
from jbrain.tasks.runner import ExecutedTurn, LoopTurnExecutor, TurnExecutor
from jbrain.tasks.scheduler import _owner_principal_id
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

NOTE_CONVERSE_KIND = "note_converse"
"""The worker dispatch key + the `ActionSpec.handler` binding."""

NOTE_CONVERSE_AGENT = "note_ingest"
"""The persona (D16, migration 0192). Hardcoded, never a parameter: the closed
allowlist is the guarantee, and a caller-supplied persona would be the door around it."""

# The READ tools this persona inherits unchanged (TOOL_SURFACE). Named here so the
# registry built below and `agents.NOTE_INGEST_TOOLS` cannot drift apart — a test pins
# the registry's names to the allowlist, because a tool in one and not the other is
# either a dead offer or an unreachable handler.
NOTE_READ_TOOLS = frozenset({"find_entity", "read_entity", "current_time"})


def note_owned_by_emr(note: NoteInfo) -> bool:
    """Whether the deterministic EMR importer owns this note's graph writes (W4/D9).

    One function, two call sites — the profile narrowing and the registry build — so the
    two locks can never disagree about which notes they cover. The markers are the ones
    migration 0122's own trigger filter uses; see `ingest/emr/ownership.py`."""
    return emr_owned(note.domain, note.destination, [a.media_type for a in note.attachments])


NOTE_CONVERSE_SPEC = ActionSpec(
    name="note_converse",
    version=1,
    handler=NOTE_CONVERSE_KIND,
    # Cross-domain like every other note action: the note carries its own domain and
    # the conversation is opened for whatever note arrived, so gating the action on a
    # domain scope would simply drop health/finance notes on the floor.
    domain_optional=True,
    # It writes: an agent_sessions row, a run, a transcript, and the conversation +
    # ledger rows. It writes no GRAPH in W2 — the persona has no tools — but `mutating`
    # describes blast radius, not usefulness, and W3 hangs the graph writes here.
    mutating=True,
    # One `agent.turn` per note on a serial GPU. `integrate_note` is already
    # `expensive` for strictly less model work than this.
    cost_class="expensive",
    # A note must never end up with two conversations. The advisory hint here names
    # the payload key; the enforcement is the partial unique index (0191) with the
    # dispatcher's graceful skip in front of it (workflow/dispatcher._already_active).
    dedup_key_expr="note_id",
    description="Open the note's agent conversation and read the note in it.",
    category="note",
)

# The per-turn half of the data boundary (`intake/turn.py:_RECIPIENT_FRAME`). The
# standing rule lives in the persona prompt ("THE NOTE IS DATA"); this demotes THIS
# turn's content, so an injected "ignore your instructions" arrives already labelled
# as material the model was told to describe rather than obey.
#
# CLOSED, and closed with a per-turn NONCE — which is where this departs from
# `intake/turn.py`'s frame, deliberately. That persona holds no tools in any wave; this
# one is the seat W3 puts the graph writes in. An open-ended prefix is impersonable: a
# body can write its own "(end of captured note)" and then its own `[CAPTURED NOTE ...]`
# header, and nothing in the text tells the model which header the SYSTEM wrote. A
# random tag the body cannot predict makes the boundary checkable instead of
# conventional — the same reason a heredoc delimiter is random when the payload is
# untrusted. Cheap now, and the persona is toothless while it beds in.
#
# The frame itself lives in `analysis/noteframe.py` now that W3 gave it a second caller:
# `readtools.read_note` fences a FETCHED note the same way on the on-reply turn, and one
# boundary the model meets twice is worth more than two it has to tell apart.

# The lifecycle endings live with the state machine now (`state_for_stop`): a clean turn
# `settled`, an `ask_owner` turn `waiting_on_owner`, anything else `failed`. Not cosmetic
# — plan constraint 6 says the whole-note sweep must run on neither a truncated pass nor
# a waiting one, and this distinction is what it keys on.

_TITLE_LEN = 60


def capture_line(note: NoteInfo) -> str:
    """When the note was captured, in the zone it was captured in.

    The client records its UTC offset at capture (`tz_offset_minutes`); a note written
    at 11pm local must not read as the next day, which is exactly the kind of one-day
    slip a dated fact in the graph would carry forever. Falls back to UTC for the
    server-stamped and pre-Phase-3 rows that recorded no offset."""
    offset = note.tz_offset_minutes
    if offset is None:
        return f"{note.created_at.astimezone(UTC):%A, %B %d, %Y, %H:%M} UTC"
    local = note.created_at.astimezone(UTC) + timedelta(minutes=offset)
    sign, mins = ("+", offset) if offset >= 0 else ("-", -offset)
    return f"{local:%A, %B %d, %Y, %H:%M} (UTC{sign}{mins // 60:02d}:{mins % 60:02d})"


def note_read_scopes(profile: AgentProfile, note: NoteInfo) -> tuple[str, ...]:
    """The RLS read scope a note conversation's TOOLS run under (plan constraint 2):
    the note's own domain plus `general`, and nothing else — a health note's thread can
    read health and general entities, never finance.

    The one place this is computed. It is also what is stored as the session row's
    `domain_scopes`, but the UNATTENDED pass recomputes it FROM THE NOTE rather than
    reading the row back: the row is metadata an owner-facing route could once have
    rewritten (now refused — `AgentSessionRepo.set_scopes`), and W2-era rows carry `[]`
    from when the persona read nothing. Deriving it here means neither can widen a pass.

    The owner's REPLY turn does read the stored row — it is an ordinary `/chat` turn
    (`api/agent.py`), which knows a session and not a note. That is safe in the only
    direction that matters: for a thread this function opened the row IS what it
    returned, and a W2-era row is `[]`, which starves a turn rather than widening one.

    A `reads_knowledge_base=False` profile gets EMPTY scopes, which is a firewall rather
    than a flag: it can then read no domain row at all.

    Note what this does NOT scope: the graph WRITE path. `graphwritetools` runs its
    commits at full owner scope because layer 1 of resolution carries no domain
    predicate (narrowing it mints duplicates) and a floored fact write would be refused
    outright. These scopes bound what the conversation may READ and be told."""
    return (note.domain, "general") if profile.reads_knowledge_base else ()


def _title(note: NoteInfo) -> str:
    """The thread's name in the Chats list. The note's own first line, clamped — a
    conversation about a note should be findable by the note, not by "Note 3f2a…"."""
    first = next((line.strip() for line in note.body.splitlines() if line.strip()), "")
    return (first[:_TITLE_LEN] or "Note").strip()


@dataclass
class NoteConverseRunner:
    """Runs one note's conversation. Constructed once in the worker; `note_converse`
    is the registered action handler."""

    maker: async_sessionmaker[AsyncSession]
    notes: NotesRepo
    sessions: AgentSessionRepo
    runlog: AgentRunLog
    transcript: AgentTranscript
    executor: TurnExecutor
    owner_principal_id: Callable[[], Awaitable[str | None]]
    conversations: NoteConversationRepo = field(default_factory=NoteConversationRepo)
    prefs: OwnerPrefsRepo = field(default_factory=OwnerPrefsRepo)
    # The settle the pass runs at its end (S2/S3): `settle_tail` so its writes finally
    # project, and `sweep_note` so its `conversation` claim is finally released. Not the
    # stamp and not the state flip — see `clarify.settle_conversation`.
    #
    # None keeps W2's behaviour, which is what the tests that fake a turn with no write
    # tools use: a pass that CANNOT write has nothing to project and nothing to release.
    # `note_converse_handler` always sets it, so no production path runs without one.
    pipeline: AnalysisPipeline | None = None
    # Builds the turn executor for ONE note, so the graph-write tools can be BOUND to
    # that note (W3): `resolve_entity`/`assert_fact` take no note id from the model —
    # a write primitive a hostile body could point at another note is not a tool, it is
    # a hole. None keeps W2's behaviour (the fixed `executor` above, whose registry is
    # empty), which is what the tests that fake a turn use.
    executor_for_note: Callable[[NoteInfo, Sequence[str]], TurnExecutor] | None = None

    async def note_converse(self, payload: dict[str, Any]) -> object:
        """Open the note's conversation, read the note in it, and settle it.

        Fail-closed and idempotent at every step a duplicate event can reach: a missing
        note, a note that already has a live thread, or a lost race against the partial
        unique index all return quietly rather than raising, because a raise here is a
        retried worker job and a 500 in the run log for something that is not wrong."""
        note_id = str(payload.get("note_id") or "").strip()
        if not note_id:
            return None
        owner_pid = await self.owner_principal_id()
        if owner_pid is None:
            log.warning("note_converse.no_owner", note_id=note_id)
            return None
        owner_ctx = SessionContext(principal_id=owner_pid, principal_kind="owner")

        # The notes READ PATH, never `Note.body` off the ORM: D6 makes a note's text its
        # frozen body plus its appended clarification blocks, and composing that is the
        # repo's job. Reading through it means turn 0 picks composition up for free the
        # day it lands.
        note = await self.notes.get_note(owner_ctx, note_id)
        if note is None:
            return None

        async with scoped_session(self.maker, owner_ctx) as s:
            if await self.conversations.live_for_note(s, note_id) is not None:
                # The dispatcher's dedup arm normally catches this; a re-delivered event
                # that slipped past it is a skip, not a failure.
                log.info("note_converse.already_live", note_id=note_id)
                return None

        # W4's two narrowings, both halves, in that order. Neither is exclusive of the
        # other and a note can be BOTH: an approved intake submission enacting into a
        # health `Records` note with an EMR-shaped attachment is third-party-bodied AND
        # importer-owned, and such a note must end up with the INTERSECTION — the entity
        # reads and the clock, no write verb and no `ask_owner`. That is a property of
        # `narrow_for_third_party_note` intersecting rather than assigning, so the order
        # here is the documented one rather than the load-bearing one.
        #
        # W4/D9: on an EMR note the persona keeps its reads and `ask_owner` and loses
        # every graph-write verb — the deterministic parse owns those facts, and it owns
        # this note's one whole-note settle (constraint 6).
        #
        # D10 / plan risk 1: a note whose body the OWNER DID NOT WRITE runs on the
        # third-party set — the same graph writes, no `ask_owner`. The port needed no new
        # trigger (`note.ingested` has been opening this conversation over an
        # `untrusted_origin` note since W2); it needed this line, and the registry below
        # that declines to bind the handler.
        profile = agent_for(NOTE_CONVERSE_AGENT)
        if note_owned_by_emr(note):
            profile = narrow_for_emr(profile)
        if is_third_party(note.provenance):
            profile = narrow_for_third_party_note(profile)
        read_scopes = note_read_scopes(profile, note)
        # ONE transaction for the session row and the conversation row that gives it
        # meaning. The one-live index can refuse the second, and a session opened in a
        # transaction of its own would survive that refusal as an orphan — an empty
        # note_ingest thread sitting in the owner's chat list, removable only by a
        # compensating delete that can itself fail. Here the rollback takes both, so
        # there is no cleanup path to get wrong and none to leave silent.
        try:
            async with scoped_session(self.maker, owner_ctx) as s:
                session = await self.sessions.create_on(
                    s,
                    owner_ctx,
                    domain_scopes=list(read_scopes),
                    title=_title(note),
                    agent=NOTE_CONVERSE_AGENT,
                )
                await self.conversations.start(
                    s,
                    session_id=session.id,
                    note_id=note_id,
                    body_sha=note_body_sha(note.body),
                )
        except IntegrityError:
            # Lost the race to the one-live index: the note is already being read.
            log.info("note_converse.lost_race", note_id=note_id)
            return None

        await self._run_turn(owner_ctx, profile, note, session.id, read_scopes)
        return None

    async def _run_turn(
        self,
        owner_ctx: SessionContext,
        profile: AgentProfile,
        note: NoteInfo,
        session_id: str,
        read_scopes: Sequence[str],
    ) -> None:
        # Same fence, same nonce, one word about whose text it is (D10). The nonce is
        # drawn FROM THE BODY, so a submitter cannot predict the delimiter and cannot
        # forge one either — which is the property that matters most on the one note
        # whose author is known to be somebody else.
        turn_0 = framed_note(
            note.body,
            captured=capture_line(note),
            about=THIRD_PARTY_ABOUT if is_third_party(note.provenance) else OWN_NOTE_ABOUT,
        )
        run_id = await self.runlog.start(
            owner_ctx, session_id=session_id, prompt_version=profile.version
        )
        # The clock line leads (the same data-framed grounding /chat and the task runner
        # prepend) so "last Tuesday" in a note resolves without a tool the persona does
        # not have. Only the note is recorded as the user turn: the clock is scaffolding,
        # not something the owner said.
        conversation = [UserMessage(text=now_block(None)), UserMessage(text=turn_0)]

        status, stop_reason, steps, cost = "error", "error", 0, 0
        state = "failed"
        ran = False
        try:
            # The owner's standing instructions (D15), read INSIDE the try so a DB blip
            # lands the conversation `failed` — the shipped path for "this pass did not
            # finish" — instead of raising out of the job and retrying forever. Failing
            # closed is the right direction: a pass that ignored the owner's rules and
            # settled anyway would write the graph the way he asked it not to, and the
            # `integrate_note` pipeline is still writing beside this one (D13), so a
            # failed conversation costs a thread, not the note.
            profile = replace(
                profile,
                prompt=with_standing_instructions(profile.prompt, await self._rules(owner_ctx)),
            )
            # The hard turn ceiling. `LoopTurnExecutor` has none of its own — the one in
            # the repo lives in `api/agent.py`, around the /chat stream — and this is the
            # first handler to drive a full ReAct turn from the worker. Without it a
            # wedged model holds a `running` conversation open indefinitely, which is
            # exactly the state that suppresses every future pass over the note. The
            # reclaim below is the backstop for a KILLED worker; this is the bound for a
            # worker that is still alive and getting nowhere.
            executor = (
                self.executor
                if self.executor_for_note is None
                else self.executor_for_note(note, read_scopes)
            )
            async with asyncio.timeout(NOTE_TURN_WALL_CLOCK.total_seconds()):
                executed = await executor.run_turn(
                    profile=profile,
                    read_ctx=read_context(owner_ctx.principal_id, read_scopes),
                    read_scopes=read_scopes,
                    conversation=conversation,
                    timezone=None,
                    recorder=self.runlog.bound(owner_ctx, run_id),
                    agent_session_id=session_id,
                )
            result = executed.result
            ran = True
            # The meter is what the turn COST, true whatever happens next.
            steps, cost, stop_reason = result.steps, result.cost_tokens, result.stop_reason
            # Persist BEFORE settling, never after. `settled` is a claim that the pass
            # finished and its writes are on the record — constraint 6 hangs a whole-note
            # retraction off it in W3, keyed on a ledger that lives in `_record`. Latched
            # first, a `_record` that raised left `settled` + `done` with an empty
            # transcript and an empty ledger, which reads as "the agent decided this note
            # says nothing" and arms a retraction of the note's entire graph.
            await self._record(owner_ctx, session_id, run_id, turn_0, executed)
            status = "done"
            # A clean turn settles; a turn `ask_owner` ended waits; everything else —
            # truncated, out of budget, too many tool errors — fails. Constraint 6: the
            # sweep W3 hangs off `settled` must never see a turn that asserted only a
            # prefix, and must never see one that stopped to ask a question either.
            #
            # The state is derived from the STOP REASON, not from "did a tool fire", and
            # the handler has already written `waiting_on_owner` itself. Both, on purpose:
            # the handler's write is what makes the question durable the moment it is
            # asked (the owner can reply before this line runs), and this mapping is what
            # stops the settle below overwriting it — a `set_state("settled")` over a
            # waiting thread is refused by the repo, which would leave the pass raising
            # and retrying against a note that is simply waiting for an answer.
            state = state_for_stop(stop_reason)
        except TimeoutError:
            log.warning(
                "note_converse.turn_timeout",
                session_id=session_id,
                limit_s=NOTE_TURN_WALL_CLOCK.total_seconds(),
            )
            stop_reason = "turn_timeout"
        except Exception as exc:  # noqa: BLE001 — a dead pass is a `failed` thread, not a crash
            log.warning(
                "note_converse.turn_failed", session_id=session_id, ran=ran, error=repr(exc)
            )
            # A turn that RAN and then failed to PERSIST is a different fault from one
            # that never ran, and the run log is the only place it shows. Either way the
            # pass did not finish, so the status stays `error` and the state `failed`.
            if ran:
                stop_reason = "record_failed"

        with contextlib.suppress(Exception):
            await self.runlog.finish(
                owner_ctx,
                run_id,
                status=status,
                stop_reason=stop_reason,
                step_count=steps,
                cost_tokens=cost,
            )
        # The state transition is NOT best-effort: a conversation stuck in `running`
        # holds the note's one live slot forever, and nothing on a terminal-less box can
        # release it (CLAUDE.md #10). If even this fails the job raises and retries.
        async with scoped_session(self.maker, owner_ctx) as s:
            current = await self.conversations.get(s, session_id)
            asked = current is not None and current.state == "waiting_on_owner"
            if asked and state != "waiting_on_owner":
                # The turn asked, and then something after the ask went wrong (the classic
                # one is `_record` raising, which lands here as `failed`). The question
                # STANDS: it is already recorded and the owner may already be typing an
                # answer, and `_ALLOWED_SOURCES` makes dropping one spell `abandon_question`
                # precisely so a failure that means nothing of the sort cannot do it
                # silently. Writing `failed` here would also raise — leaving the job to
                # retry a note whose only problem is that it is waiting for an answer.
                log.warning(
                    "note_converse.question_stands",
                    session_id=session_id,
                    would_have_set=state,
                    stop_reason=stop_reason,
                )
                state = "waiting_on_owner"
            else:
                await self.conversations.set_state(s, session_id, state)
        # AFTER the state block, never before it: the `question_stands` branch above can
        # still turn a `settled` verdict into `waiting_on_owner`, and a settle run ahead
        # of it would have projected (and, since S3, swept) a pass that is in fact still
        # waiting for the owner. `state` here is what the database now says.
        if self.pipeline is not None:
            await settle_conversation(
                self.maker,
                owner_ctx,
                self.pipeline,
                session_id=session_id,
                state=state,
            )
        with contextlib.suppress(Exception):
            await self.sessions.touch(owner_ctx, session_id)
        log.info("note_converse.settled", session_id=session_id, state=state, steps=steps)

    async def _rules(self, owner_ctx: SessionContext) -> list[str]:
        """The owner's standing instructions for this pass (D15).

        Read once, at turn assembly, on the owner's own scope — `owner_prefs` is
        owner-only RLS, so the read is the firewall's, not this method's. It goes into
        the SYSTEM prompt rather than a message: a rule is a rule for the whole turn,
        including the reply turns W3 adds, and a message ahead of turn 0 would sit in
        the same register as the framed note it is supposed to outrank."""
        async with scoped_session(self.maker, owner_ctx) as s:
            return await self.prefs.read_rules(s, owner_ctx.principal_id or "")

    async def _record(
        self,
        owner_ctx: SessionContext,
        session_id: str,
        run_id: str,
        turn_0: str,
        executed: ExecutedTurn,
    ) -> None:
        """Persist the exchange, the ledger, and the meter seed.

        Ledger first: a call is recorded as it happened, before the assistant turn
        exists, then bound to it BY ID (`clarify.bind_turn_writes` — binding whatever is
        unbound would let an earlier turn that died mid-flight have its calls adopted by
        this one). The recorder itself is shared with the OWNER REPLY turn, which reaches
        the same two calls from `api/agent.py`: the two turn paths of one conversation
        record identically, which is what makes `NoteConversationRepo.writes()` a
        whole-conversation union rather than a whole-pass one.

        These are separate transactions, and deliberately so — the transcript store owns
        its own. So a partial IS reachable: ledger rows with no assistant turn to bind
        to. What the caller guarantees is the direction that matters — this raising means
        the conversation lands `failed`, so `settled` never stands over a record that did
        not land, and constraint 6's sweep (which fires only on `settled`) never reads a
        half-written ledger. The reply path answers the same question the same way, by
        degrading its own close to `record_failed`. The reverse is not guaranteed and does
        not need to be: a `failed` conversation's ledger is evidence, not an input to
        anything."""
        call_ids = await record_turn_writes(
            self.maker, owner_ctx, session_id=session_id, tool_steps=executed.tools
        )
        await self.transcript.record_exchange(
            owner_ctx,
            session_id=session_id,
            run_id=run_id,
            user_text=turn_0,
            assistant_text=executed.result.text,
            tools=executed.tools,
            reasoning=executed.reasoning,
        )
        await bind_turn_writes(
            self.maker, owner_ctx, session_id=session_id, run_id=run_id, call_ids=call_ids
        )
        if executed.context_window and executed.context_used:
            with contextlib.suppress(Exception):
                await self.sessions.record_context(
                    owner_ctx, session_id, executed.context_used, executed.context_window
                )


def note_converse_handler(
    maker: async_sessionmaker[AsyncSession],
    router: LlmRouter,
    *,
    pipeline: AnalysisPipeline | None = None,
) -> Callable[[dict[str, Any]], Awaitable[object]]:
    """The registered `note_converse` handler, wired for the worker.

    The registry is built PER NOTE and holds six tools: the two graph writes bound to
    this note, `ask_owner`, the two entity reads inherited unchanged, and the clock. W4
    takes tools OFF that list per note, and the two subtractions are independent: FOUR of
    the six on a note the deterministic EMR importer owns, where neither graph write is
    bound at all (D9, `note_owned_by_emr` below); FIVE on a note whose body the owner did
    not write, where `ask_owner` is left unbound (D10); and THREE — the two entity reads
    and the clock — on a note that is both. Not the chat registry — not even a filtered
    view of it. Two reasons, and the second is the one that makes it structural rather
    than tidy:

    - a graph-write handler is bound to ONE note (its id, domain, chunks and handle
      table live in the writer), so there is no chat-session copy of it to filter down
      to. `readtools.build_registry` drops both sidecars outright for that reason;
    - it is assembled from an explicit list of NAMES rather than from `load_registry`'s
      directory scan, so "this persona reaches nothing else" is a property of what was
      BUILT, not of one `frozenset` field — and D16's allowlist, a second lock over the
      same six, still says which of them it may call. A tool reaches this persona only
      by being named in BOTH places, and the whole chat tool set (with its blobs,
      search and vision clients) still never enters the worker.

    `ask_owner` is the one of the six that is ALSO on the chat registry: the unattended
    first pass runs here, in the worker, but the owner's REPLY into the same thread
    arrives as an ordinary /chat turn (D8) and the agent may still be unable to proceed
    after it. It is not note-bound the way the graph writes are — it finds its
    conversation through `ToolContext.agent_session_id` — so one handler serves both.

    `pipeline` is the shared `AnalysisPipeline` (the worker's, with its embedder and
    settings store); one is built here when a caller has none, which is the harness case
    — resolution then runs without embedding layer 2, exactly as `integrate_note` does
    on a box with no embed client."""
    analyzer = pipeline if pipeline is not None else AnalysisPipeline(maker, router)
    entities = SqlAnalysisRepo(maker)
    tools_dir = Path(readtools.__file__).parent / "tools"
    # find_entity / read_entity / current_time, INHERITED UNCHANGED (TOOL_SURFACE): the
    # agent has to be able to see what the graph already says about a name before it
    # asserts against it, and to resolve "this morning" without guessing. They read
    # under the turn's narrowed `(note_domain, 'general')` scope — which is only
    # reachable at all because `note_ingest` now reads the knowledge base.
    inherited: dict[str, Any] = {
        **build_entity_handlers(entities),
        **build_clock_handlers(),
    }
    inherited = {k: v for k, v in inherited.items() if k in NOTE_READ_TOOLS}
    ask_owner = build_ask_owner_handlers(maker)

    def executor_for_note(note: NoteInfo, read_scopes: Sequence[str]) -> TurnExecutor:
        writer = NoteGraphWriter(
            maker,
            analyzer,
            target=NoteTarget(
                note_id=uuid.UUID(note.id),
                domain=note.domain,
                captured_at=note.created_at,
                tz_offset_minutes=note.tz_offset_minutes,
                # Read from the note row, like `integrate_note` read it: an
                # `owner_correction` note's attested facts force-supersede + pin
                # (`NoteTarget.is_correction`). This is the same value `is_third_party`
                # below already reads, so the conversation now branches on provenance in
                # two directions off one field — narrower for a stranger, authoritative
                # for the owner's own correction.
                provenance=note.provenance,
            ),
            # The WRITE session is the owner at FULL scope, like `integrate_note`'s:
            # entity resolution layer 1 carries no domain predicate (narrowing mints
            # duplicates) and a floored fact write would be refused by RLS outright
            # (plan constraint 2). `read_scopes` is passed separately so the writer
            # withholds a cross-domain entity's NAME from the result text.
            write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
            read_scopes=read_scopes,
        )
        # W4's enforcement, both halves, and the reason it is HERE rather than in the
        # allowlist alone (constraint 9 / TOOL_SURFACE R2): a name with no handler behind
        # it cannot dispatch however the profile is resolved, so each narrowing gets a
        # second lock that fails independently of the first.
        #
        # D10: on a note the owner did not write, `ask_owner` is not BOUND — its sidecar
        # is never loaded, the model is never offered the verb, and there is no handler
        # for a later allowlist edit to make callable.
        # D9: an EMR note's facts are the importer's, so this registry binds no
        # graph-write handler for one. The narrowed profile says the same (the runner
        # applies `narrow_for_emr` before the turn).
        #
        # The two conditions are independent, so a note that is both keeps only the two
        # entity reads and the clock — matching the intersected allowlist exactly.
        toolset = NoteToolset(
            writer=writer,
            inherited=inherited if is_third_party(note.provenance) else inherited | ask_owner,
            writes_graph=not note_owned_by_emr(note),
        )
        return LoopTurnExecutor(router, note_registry(tools_dir, toolset.handlers()))

    runner = NoteConverseRunner(
        maker,
        notes=SqlNotesRepo(maker),
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=AgentTranscript(maker),
        # Never used once `executor_for_note` is set, which the line below always does.
        # An EMPTY registry keeps the fallback inert rather than accidentally permissive:
        # a fallback holding the tools would be a second, unbound path to them.
        executor=LoopTurnExecutor(router, ToolRegistry(())),
        owner_principal_id=lambda: _owner_principal_id(maker),
        executor_for_note=executor_for_note,
        # The SAME pipeline the write tools commit through, so the end-of-pass settle
        # projects with the worker's embedder and settings store rather than a second,
        # thinner one.
        pipeline=analyzer,
    )
    return runner.note_converse
