"""Persisting a session's conversation transcript so reopening Full Brain replays
the same exchange (docs/reference/ASSISTANT.md "Sessions").

Owner-only, append-only: one `agent_turns` row per turn. A completed turn writes
the user message then the assistant answer (with the tool sources it surfaced) in
one transaction; `load` returns them in order for the PWA to seed the surface.
Recording is best-effort at the call site — a write failure never breaks a turn.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.attachments import AttachmentInfo, TurnAttachmentRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.agent import AgentTurn

# How far back an earlier image is carried forward inline so a vision-capable turn can
# re-see it without an analyze_image round-trip (docs/reference/ASSISTANT.md). A UNION, not
# an intersection: the last CARRY_TURN_WINDOW user turns (so a rapid back-and-forth keeps the
# picture in view) OR any user turn within CARRY_TIME_WINDOW (so a slow-but-recent discussion
# keeps it too). An image drops from view only once it is BOTH more than N turns back AND
# older than the time window — bounding the re-paid vision tokens.
CARRY_TURN_WINDOW = 4
CARRY_TIME_WINDOW = timedelta(minutes=15)
# A ceiling on the user turns scanned for the time-window arm, so a pathological session
# can't unbounded-scan. Far above any real rate (200 user turns inside 15 minutes).
_CARRY_SCAN_LIMIT = 200


def _carry_forward_turn_ids(
    rows: Sequence[tuple[Any, datetime | None]],
    *,
    now: datetime,
    turn_window: int = CARRY_TURN_WINDOW,
    time_window: timedelta = CARRY_TIME_WINDOW,
) -> list[str]:
    """From the session's user turns NEWEST-first (by seq) as `(id, created_at)` pairs, the ids
    eligible to carry an image forward: the newest `turn_window` (always) UNION any whose
    `created_at` is within `time_window` of `now`. Returned OLDEST-first so re-injected images
    read chronologically. Pure — the DB query wraps it; unit-tested for the union/boundary."""
    cutoff = now - time_window
    eligible = [
        turn_id
        for i, (turn_id, created_at) in enumerate(rows)
        if i < turn_window or (created_at is not None and created_at >= cutoff)
    ]
    return [str(turn_id) for turn_id in reversed(eligible)]


@dataclass(frozen=True)
class TurnRecord:
    role: str  # user | assistant
    content: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""  # the assistant turn's thinking trace (gpt-oss/GLM); "" otherwise
    # The chat files bound to a USER turn (Stage-2 Wave 2), replayed as attachment
    # chips; always empty for an assistant turn.
    attachments: list[AttachmentInfo] = field(default_factory=list)


class AgentTranscript:
    """Reads/writes the per-session transcript on owner-scoped sessions."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        attachments: TurnAttachmentRepo | None = None,
    ):
        self._maker = maker
        # Optional so existing callers/tests that predate chat attachments still
        # construct the transcript; when absent, `load` simply returns no attachments.
        self._attachments = attachments

    async def record_exchange(
        self,
        ctx: SessionContext,
        *,
        session_id: str,
        run_id: str | None,
        user_text: str,
        assistant_text: str,
        tools: Sequence[dict[str, Any]],
        reasoning: str = "",
    ) -> str:
        """Append the user turn then the assistant turn for one completed exchange.
        Returns the new USER turn's id so the caller can bind the turn's pre-uploaded
        attachments to it (the attachment row is owner-scoped on its own firewall, so
        binding runs separately under the session's narrowed context)."""
        sid = uuid.UUID(session_id)
        rid = uuid.UUID(run_id) if run_id is not None else None
        async with scoped_session(self._maker, ctx) as session:
            user_turn = AgentTurn(
                session_id=sid, run_id=rid, role="user", content=user_text, tools=[]
            )
            session.add(user_turn)
            session.add(
                AgentTurn(
                    session_id=sid,
                    run_id=rid,
                    role="assistant",
                    content=assistant_text,
                    tools=list(tools),
                    reasoning=reasoning,
                )
            )
            await session.flush()
            return str(user_turn.id)

    async def record_answer(
        self,
        ctx: SessionContext,
        *,
        session_id: str,
        run_id: str | None,
        assistant_text: str,
        tools: Sequence[dict[str, Any]],
        reasoning: str = "",
    ) -> None:
        """Append ONLY an assistant turn — no user turn — for a completion the owner didn't
        type. The deferred-analysis auto-resume is driven by a server-authored system notice,
        not owner input, so persisting it as a user turn would replay a pseudo-owner bubble on
        reopen; the answer stands on its own after the analysis card."""
        async with scoped_session(self._maker, ctx) as session:
            session.add(
                AgentTurn(
                    session_id=uuid.UUID(session_id),
                    run_id=uuid.UUID(run_id) if run_id is not None else None,
                    role="assistant",
                    content=assistant_text,
                    tools=list(tools),
                    reasoning=reasoning,
                )
            )

    async def recent_image_turns(
        self, ctx: SessionContext, session_id: str, *, now: datetime
    ) -> list[tuple[str, list[AttachmentInfo]]]:
        """`(turn content, image attachments)` for the session's RECENT user turns that
        carried an image — what a vision-capable follow-up turn should still see. "Recent"
        is the `_carry_forward_turn_ids` union (last N turns OR within the time window).
        The content is the key the caller uses to ANCHOR the image at its own turn in the
        client-supplied history, so the bytes sit at a byte-stable position the gateway's
        KV prefix cache can hold through (llama-server matches a media chunk by content
        hash + position) instead of re-encoding every turn. Oldest-first; turns without an
        image are dropped; images only (a re-injected PDF's pages would be heavy; the
        owner's case is a picture). Empty when this transcript has no attachment repo, or
        the session has no recent image."""
        if self._attachments is None:
            return []
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    select(AgentTurn.id, AgentTurn.created_at, AgentTurn.content)
                    .where(
                        AgentTurn.session_id == uuid.UUID(session_id),
                        AgentTurn.role == "user",
                    )
                    .order_by(AgentTurn.seq.desc())
                    .limit(_CARRY_SCAN_LIMIT)
                )
            ).all()
        turn_ids = _carry_forward_turn_ids([(r[0], r[1]) for r in rows], now=now)
        if not turn_ids:
            return []
        content_by_id = {str(r[0]): r[2] for r in rows}
        by_turn = await self._attachments.list_for_turns(ctx, turn_ids)
        out: list[tuple[str, list[AttachmentInfo]]] = []
        for tid in turn_ids:  # oldest-first, so the images read chronologically
            images = [i for i in by_turn.get(tid, []) if i.media_type.startswith("image/")]
            if images:
                out.append((content_by_id.get(tid, ""), images))
        return out

    async def load(self, ctx: SessionContext, session_id: str) -> list[TurnRecord]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                (
                    await session.execute(
                        select(AgentTurn)
                        .where(AgentTurn.session_id == uuid.UUID(session_id))
                        .order_by(AgentTurn.seq)
                    )
                )
                .scalars()
                .all()
            )
        # One RLS-scoped round-trip for every user turn's attachments, so a reopened
        # session replays the files on the turn that carried them.
        by_turn: dict[str, list[AttachmentInfo]] = {}
        if self._attachments is not None:
            user_turn_ids = [str(r.id) for r in rows if r.role == "user"]
            by_turn = await self._attachments.list_for_turns(ctx, user_turn_ids)
        return [
            TurnRecord(
                role=r.role,
                content=r.content,
                tools=list(r.tools),
                reasoning=r.reasoning,
                attachments=by_turn.get(str(r.id), []),
            )
            for r in rows
        ]
