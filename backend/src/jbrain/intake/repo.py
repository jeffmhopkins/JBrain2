"""SQL implementation of the guided-intake repository.

Owner methods run on the caller's owner-scoped session (the RLS firewall is
Postgres', via `app.is_full_owner()`). `claim` is the one pre-principal path: it
runs under the `bootstrap` auth context — the same carve-out the auth repo uses —
so it can read a link by secret and bind a per-session principal before any
principal context exists.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, intake_context, scoped_session
from jbrain.intake.service import (
    ClaimResult,
    IntakeLinkConfig,
    IntakeLinkRecord,
    IntakeSessionRecord,
    IntakeSessionState,
    IntakeSubmissionRecord,
)
from jbrain.models import IntakeLink, IntakeSession, IntakeSubmission, Principal

_BOOTSTRAP = SessionContext(auth_context="bootstrap")


class SqlIntakeRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def create_link(
        self, ctx: SessionContext, *, secret_hash: str, config: IntakeLinkConfig
    ) -> IntakeLinkRecord:
        expires_at = datetime.now(UTC) + timedelta(hours=config.ttl_hours)
        async with scoped_session(self._maker, ctx) as session:
            row = IntakeLink(
                subject_id=uuid.UUID(config.subject_id),
                domain_code=config.domain_code,
                label=config.label,
                persona_brief=config.persona_brief,
                fields_brief=config.fields_brief,
                opening_blurb=config.opening_blurb,
                max_runs=config.max_runs,
                max_opens=config.max_opens,
                bind_on_first=config.bind_on_first,
                capture_enterer_name=config.capture_enterer_name,
                disclose_owner_identity=config.disclose_owner_identity,
                secret_hash=secret_hash,
                expires_at=expires_at,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _link_record(row)

    async def list_links(self, ctx: SessionContext) -> list[IntakeLinkRecord]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(select(IntakeLink).order_by(IntakeLink.created_at.desc()))
            ).scalars()
            return [_link_record(r) for r in rows]

    async def get_link(self, ctx: SessionContext, link_id: str) -> IntakeLinkRecord | None:
        try:
            lid = uuid.UUID(link_id)
        except ValueError:
            return None
        async with scoped_session(self._maker, ctx) as session:
            row = await session.get(IntakeLink, lid)
            return _link_record(row) if row is not None else None

    async def revoke_link(self, ctx: SessionContext, link_id: str) -> bool:
        """Flip an active link to `revoked` and cascade-revoke its in-flight session
        principals, so every redeemed cookie fails closed on its next request. No-op on
        an unknown / already-revoked id."""
        try:
            lid = uuid.UUID(link_id)
        except ValueError:
            return False
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                update(IntakeLink)
                .where(IntakeLink.id == lid, IntakeLink.status == "active")
                .values(status="revoked")
            )
            if (cast("CursorResult[Any]", result).rowcount or 0) == 0:
                return False
            # Kill in-flight cookies: a redeemed session's principal stops
            # authenticating the moment the link is revoked (principals UPDATE policy
            # admits the owner via is_owner()).
            await session.execute(
                text(
                    "UPDATE app.principals SET revoked_at = now()"
                    " WHERE kind = 'intake_link' AND revoked_at IS NULL"
                    " AND id IN (SELECT principal_id FROM app.intake_sessions"
                    " WHERE link_id = :lid)"
                ),
                {"lid": str(lid)},
            )
            return True

    async def list_sessions(self, ctx: SessionContext, link_id: str) -> list[IntakeSessionRecord]:
        try:
            lid = uuid.UUID(link_id)
        except ValueError:
            return []
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    select(IntakeSession)
                    .where(IntakeSession.link_id == lid)
                    .order_by(IntakeSession.opened_at.desc())
                )
            ).scalars()
            return [_session_record(r) for r in rows]

    async def list_submissions(
        self, ctx: SessionContext, link_id: str
    ) -> list[IntakeSubmissionRecord]:
        try:
            lid = uuid.UUID(link_id)
        except ValueError:
            return []
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    select(IntakeSubmission)
                    .where(IntakeSubmission.link_id == lid)
                    .order_by(IntakeSubmission.created_at.desc())
                )
            ).scalars()
            return [_submission_record(r, with_transcript=False) for r in rows]

    async def get_submission(
        self, ctx: SessionContext, submission_id: str
    ) -> IntakeSubmissionRecord | None:
        try:
            sid = uuid.UUID(submission_id)
        except ValueError:
            return None
        async with scoped_session(self._maker, ctx) as session:
            row = await session.get(IntakeSubmission, sid)
            return _submission_record(row, with_transcript=True) if row is not None else None

    async def claim(
        self, *, secret_hash: str, principal_key_hash: str, label: str
    ) -> ClaimResult | None:
        """Atomically burn one open and bind a fresh per-session principal, or None.

        ONE conditional UPDATE is the gate: it matches only an active, unexpired,
        un-capped link and bumps `opens_used`, so concurrent redeems can never exceed
        the cap (max_opens, or 1 for bind-on-first). On a win — same transaction — a
        non-owner `intake_link` principal is created carrying the link's expiry (so the
        cookie dies server-side at TTL) and an `intake_sessions` row snapshots the
        config. A miss (unknown / revoked / lapsed / capped / already-bound) returns
        None with nothing written."""
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            link = (
                (
                    await session.execute(
                        text(
                            "UPDATE app.intake_links SET opens_used = opens_used + 1"
                            " WHERE secret_hash = :h"
                            "   AND status = 'active'"
                            "   AND expires_at > now()"
                            "   AND runs_used < max_runs"
                            "   AND opens_used <"
                            "     (CASE WHEN bind_on_first THEN 1 ELSE max_opens END)"
                            " RETURNING id, subject_id, domain_code, label, persona_brief,"
                            "   fields_brief, opening_blurb, capture_enterer_name,"
                            "   disclose_owner_identity, expires_at"
                        ),
                        {"h": secret_hash},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if link is None:
                return None
            principal = Principal(
                kind="intake_link",
                key_hash=principal_key_hash,
                label=label,
                expires_at=link["expires_at"],
            )
            session.add(principal)
            await session.flush()
            snapshot = {
                "link_id": str(link["id"]),
                "subject_id": str(link["subject_id"]),
                "domain_code": link["domain_code"],
                "label": link["label"],
                "persona_brief": link["persona_brief"],
                "fields_brief": link["fields_brief"],
                "opening_blurb": link["opening_blurb"],
                "capture_enterer_name": link["capture_enterer_name"],
                "disclose_owner_identity": link["disclose_owner_identity"],
            }
            isess = IntakeSession(
                principal_id=principal.id,
                link_id=link["id"],
                config_snapshot=snapshot,
                status="drafting",
            )
            session.add(isess)
            await session.flush()
            return ClaimResult(
                principal_id=str(principal.id),
                session_id=str(isess.id),
                link_id=str(link["id"]),
                config_snapshot=snapshot,
                expires_at=link["expires_at"],
            )

    async def session_state(self, principal_id: str) -> IntakeSessionState | None:
        """The recipient's own session, read under its per-session principal (the RLS pin
        returns only its own row). None if the principal has no session."""
        try:
            pid = uuid.UUID(principal_id)
        except ValueError:
            return None
        async with scoped_session(self._maker, intake_context(principal_id)) as session:
            row = (
                await session.execute(
                    select(IntakeSession).where(IntakeSession.principal_id == pid)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return IntakeSessionState(
                id=str(row.id),
                link_id=str(row.link_id),
                principal_id=str(row.principal_id),
                status=row.status,
                config_snapshot=dict(row.config_snapshot or {}),
                transcript=list(row.transcript or []),
                turns_used=row.turns_used,
                cost_tokens_used=row.cost_tokens_used,
            )

    async def record_turn(
        self,
        principal_id: str,
        session_id: str,
        *,
        recipient: str,
        assistant: str,
        cost_tokens: int,
    ) -> None:
        """Append one exchange to the running transcript and bump the per-session
        cumulative counters — under the recipient's own principal (the pin restricts the
        UPDATE to its own session)."""
        entries = json.dumps(
            [{"role": "recipient", "text": recipient}, {"role": "interviewer", "text": assistant}]
        )
        async with scoped_session(self._maker, intake_context(principal_id)) as session:
            await session.execute(
                text(
                    "UPDATE app.intake_sessions"
                    " SET transcript = transcript || CAST(:entries AS jsonb),"
                    "     turns_used = turns_used + 1,"
                    "     cost_tokens_used = cost_tokens_used + :cost,"
                    "     last_turn_at = now()"
                    " WHERE id = :sid AND principal_id = :pid"
                ),
                {
                    "entries": entries,
                    "cost": max(cost_tokens, 0),
                    "sid": session_id,
                    "pid": principal_id,
                },
            )

    async def capture(
        self,
        principal_id: str,
        session_id: str,
        link_id: str,
        *,
        enterer_name: str,
        draft: dict,
        transcript: list,
    ) -> str | None:
        """The capture-only write (#4/#10): burn one RUN on the link (the submission
        ceiling), then materialize the recipient's submission under its own principal.

        The run burn is an atomic gate under bootstrap (runs_used < max_runs); a miss
        (runs exhausted / link closed) returns None and writes nothing. On a win the
        recipient writes its OWN submission row (principal_id pin) and flips its session to
        `submitted`. This stages NO Proposal and triggers NO job — the owner materializes
        the Proposal separately (W4). The (rare) burn-succeeds / insert-fails residual is
        an over-count of one run, never an over-issue of a submission."""
        async with scoped_session(self._maker, _BOOTSTRAP) as session:
            burned = (
                await session.execute(
                    text(
                        "UPDATE app.intake_links SET runs_used = runs_used + 1"
                        " WHERE id = :lid AND status = 'active' AND runs_used < max_runs"
                        " RETURNING id"
                    ),
                    {"lid": link_id},
                )
            ).scalar_one_or_none()
        if burned is None:
            return None
        async with scoped_session(self._maker, intake_context(principal_id)) as session:
            submission = IntakeSubmission(
                link_id=uuid.UUID(link_id),
                session_id=uuid.UUID(session_id),
                principal_id=uuid.UUID(principal_id),
                enterer_name=enterer_name,
                transcript=transcript,
                draft=draft,
                status="submitted",
            )
            session.add(submission)
            await session.execute(
                text(
                    "UPDATE app.intake_sessions SET status = 'submitted'"
                    " WHERE id = :sid AND principal_id = :pid"
                ),
                {"sid": session_id, "pid": principal_id},
            )
            await session.flush()
            return str(submission.id)

    async def reap_abandoned(self, ctx: SessionContext, older_than_seconds: int) -> int:
        """Transition stale `drafting` sessions to `abandoned` (the reaper, §6). A session
        is stale if its last turn (or its open, if it never had one) is older than the
        window. An abandoned open KEEPS its opens_used slot (not reclaimed). Runs under a
        full-owner maintenance context; returns how many were reaped."""
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                text(
                    "UPDATE app.intake_sessions SET status = 'abandoned'"
                    " WHERE status = 'drafting'"
                    "   AND coalesce(last_turn_at, opened_at)"
                    "       < now() - make_interval(secs => :secs)"
                ),
                {"secs": older_than_seconds},
            )
            return cast("CursorResult[Any]", result).rowcount or 0


def _link_record(row: IntakeLink) -> IntakeLinkRecord:
    return IntakeLinkRecord(
        id=str(row.id),
        subject_id=str(row.subject_id),
        domain_code=row.domain_code,
        label=row.label,
        persona_brief=row.persona_brief,
        fields_brief=row.fields_brief,
        opening_blurb=row.opening_blurb,
        max_runs=row.max_runs,
        runs_used=row.runs_used,
        max_opens=row.max_opens,
        opens_used=row.opens_used,
        bind_on_first=row.bind_on_first,
        capture_enterer_name=row.capture_enterer_name,
        disclose_owner_identity=row.disclose_owner_identity,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _session_record(row: IntakeSession) -> IntakeSessionRecord:
    return IntakeSessionRecord(
        id=str(row.id),
        link_id=str(row.link_id),
        principal_id=str(row.principal_id),
        opened_at=row.opened_at,
        status=row.status,
        config_snapshot=dict(row.config_snapshot or {}),
    )


def _submission_record(row: IntakeSubmission, *, with_transcript: bool) -> IntakeSubmissionRecord:
    return IntakeSubmissionRecord(
        id=str(row.id),
        link_id=str(row.link_id),
        session_id=str(row.session_id),
        enterer_name=row.enterer_name,
        draft=dict(row.draft or {}),
        status=row.status,
        proposal_id=str(row.proposal_id) if row.proposal_id is not None else None,
        note_ids=[str(n) for n in (row.note_ids or [])],
        created_at=row.created_at,
        updated_at=row.updated_at,
        transcript=list(row.transcript or []) if with_transcript else None,
    )
