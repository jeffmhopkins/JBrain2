"""Guided-intake share links: owner management + public redeem (W1).

Owner routes (mint / list / get / revoke links, list sessions + submissions, read a
transcript) are owner-gated and run under the owner's full-owner RLS context. Redeem
is intentionally unauthenticated — the link secret IS the credential — and binds a
NON-owner `intake_link` principal scoped to one session (the chat turn + capture land
in W3). The show-once secret is returned only at mint (#14); to re-send, re-mint.
"""

from __future__ import annotations

from datetime import UTC, datetime  # noqa: TC003 - Pydantic needs the runtime symbol
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from jbrain.api.deps import AuthRepoDep, OwnerDep, SettingsDep
from jbrain.db.session import SessionContext
from jbrain.intake import service
from jbrain.intake.service import IntakeLinkConfig, IntakeRepo

router = APIRouter()


def get_intake_repo(request: Request) -> IntakeRepo:
    return cast(IntakeRepo, request.app.state.intake_repo)


IntakeRepoDep = Annotated[IntakeRepo, Depends(get_intake_repo)]


def _owner_ctx(principal_id: str) -> SessionContext:
    # Full owner (owner_scoped=False): is_full_owner() is true, so the management
    # routes see every link/session/submission.
    return SessionContext(principal_id=principal_id, principal_kind="owner")


class MintLinkRequest(BaseModel):
    subject_id: str = Field(min_length=1)
    domain_code: str = Field(min_length=1, max_length=64)
    fields_brief: str = Field(min_length=1, max_length=8000)
    persona_brief: str = Field(default="", max_length=8000)
    opening_blurb: str = Field(default="", max_length=8000)
    label: str = Field(default="", max_length=128)
    max_runs: int = Field(ge=1, le=1000)
    # Defaults to 4x max_runs when omitted (§4): the opens ceiling is higher than the
    # submission ceiling so abandoned opens don't starve real submissions.
    max_opens: int | None = Field(default=None, ge=1, le=10000)
    bind_on_first: bool
    capture_enterer_name: bool = True
    disclose_owner_identity: bool = False
    # 15 minutes to 30 days; a link must be time-boxed (no never-expiring grant).
    ttl_hours: float = Field(default=24.0, ge=0.25, le=24 * 30)


class MintLinkOut(BaseModel):
    id: str
    label: str
    expires_at: datetime
    # The bearer secret — shown EXACTLY once, never recoverable from the list.
    secret: str


class LinkOut(BaseModel):
    id: str
    subject_id: str
    domain_code: str
    label: str
    fields_brief: str
    persona_brief: str
    opening_blurb: str
    max_runs: int
    runs_used: int
    max_opens: int
    opens_used: int
    bind_on_first: bool
    capture_enterer_name: bool
    disclose_owner_identity: bool
    status: str
    created_at: datetime
    expires_at: datetime


class SessionOut(BaseModel):
    id: str
    link_id: str
    opened_at: datetime
    status: str


class SubmissionOut(BaseModel):
    id: str
    link_id: str
    session_id: str
    enterer_name: str
    draft: dict
    status: str
    proposal_id: str | None
    note_ids: list[str]
    created_at: datetime
    updated_at: datetime


class SubmissionDetailOut(SubmissionOut):
    transcript: list


class RedeemRequest(BaseModel):
    secret: str = Field(min_length=1)


class RedeemOut(BaseModel):
    session_id: str
    link_id: str
    opening_blurb: str
    capture_enterer_name: bool
    disclose_owner_identity: bool


def _link_out(r: service.IntakeLinkRecord) -> LinkOut:
    return LinkOut(
        id=r.id,
        subject_id=r.subject_id,
        domain_code=r.domain_code,
        label=r.label,
        fields_brief=r.fields_brief,
        persona_brief=r.persona_brief,
        opening_blurb=r.opening_blurb,
        max_runs=r.max_runs,
        runs_used=r.runs_used,
        max_opens=r.max_opens,
        opens_used=r.opens_used,
        bind_on_first=r.bind_on_first,
        capture_enterer_name=r.capture_enterer_name,
        disclose_owner_identity=r.disclose_owner_identity,
        status=r.status,
        created_at=r.created_at,
        expires_at=r.expires_at,
    )


def _submission_out(r: service.IntakeSubmissionRecord) -> SubmissionOut:
    return SubmissionOut(
        id=r.id,
        link_id=r.link_id,
        session_id=r.session_id,
        enterer_name=r.enterer_name,
        draft=r.draft,
        status=r.status,
        proposal_id=r.proposal_id,
        note_ids=r.note_ids,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.post("/intake/links", status_code=201)
async def mint_link(body: MintLinkRequest, owner: OwnerDep, repo: IntakeRepoDep) -> MintLinkOut:
    """Mint a guided-intake link (owner only). Returns the secret exactly once.

    The user-facing path mints via an agent-staged, owner-approved Proposal (W4); this
    direct route is the owner-only primitive that path calls on approval."""
    config = IntakeLinkConfig(
        subject_id=body.subject_id,
        domain_code=body.domain_code,
        label=body.label,
        persona_brief=body.persona_brief,
        fields_brief=body.fields_brief,
        opening_blurb=body.opening_blurb,
        max_runs=body.max_runs,
        max_opens=body.max_opens if body.max_opens is not None else body.max_runs * 4,
        bind_on_first=body.bind_on_first,
        ttl_hours=body.ttl_hours,
        capture_enterer_name=body.capture_enterer_name,
        disclose_owner_identity=body.disclose_owner_identity,
    )
    try:
        secret, record = await service.mint_intake_link(repo, _owner_ctx(owner.id), config)
    except IntegrityError as exc:
        # An unknown subject_id / domain_code trips the FK — the owner can't mint a link
        # attributed to a subject/domain that doesn't exist (firewall integrity, §7).
        raise HTTPException(status_code=400, detail="unknown subject or domain") from exc
    return MintLinkOut(
        id=record.id, label=record.label, expires_at=record.expires_at, secret=secret
    )


@router.get("/intake/links")
async def list_links(owner: OwnerDep, repo: IntakeRepoDep) -> list[LinkOut]:
    """Every link, newest first — metadata only, never a secret."""
    return [_link_out(r) for r in await repo.list_links(_owner_ctx(owner.id))]


@router.get("/intake/links/{link_id}")
async def get_link(link_id: str, owner: OwnerDep, repo: IntakeRepoDep) -> LinkOut:
    record = await repo.get_link(_owner_ctx(owner.id), link_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown link")
    return _link_out(record)


@router.delete("/intake/links/{link_id}", status_code=204)
async def revoke_link(link_id: str, owner: OwnerDep, repo: IntakeRepoDep) -> None:
    """Revoke a link (owner only). 404 on an unknown / already-revoked id."""
    if not await service.revoke_intake_link(repo, _owner_ctx(owner.id), link_id):
        raise HTTPException(status_code=404, detail="unknown link")


@router.get("/intake/links/{link_id}/sessions")
async def list_sessions(link_id: str, owner: OwnerDep, repo: IntakeRepoDep) -> list[SessionOut]:
    """The link's opened sessions (the owner's conversation browse, #15)."""
    rows = await repo.list_sessions(_owner_ctx(owner.id), link_id)
    return [
        SessionOut(id=r.id, link_id=r.link_id, opened_at=r.opened_at, status=r.status) for r in rows
    ]


@router.get("/intake/links/{link_id}/submissions")
async def list_submissions(
    link_id: str, owner: OwnerDep, repo: IntakeRepoDep
) -> list[SubmissionOut]:
    """The link's captured submissions, newest first (transcripts read separately)."""
    return [_submission_out(r) for r in await repo.list_submissions(_owner_ctx(owner.id), link_id)]


@router.get("/intake/submissions/{submission_id}")
async def get_submission(
    submission_id: str, owner: OwnerDep, repo: IntakeRepoDep
) -> SubmissionDetailOut:
    """One submission with its full transcript (the per-submission deep view, #15)."""
    record = await repo.get_submission(_owner_ctx(owner.id), submission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown submission")
    base = _submission_out(record)
    return SubmissionDetailOut(**base.model_dump(), transcript=record.transcript or [])


@router.post("/intake/redeem")
async def redeem(
    body: RedeemRequest,
    response: Response,
    repo: IntakeRepoDep,
    auth_repo: AuthRepoDep,
    settings: SettingsDep,
) -> RedeemOut:
    """Exchange a link secret for a session cookie scoped to a fresh non-owner principal.

    401 on an invalid / revoked / lapsed / capped secret, writing no cookie.
    Unauthenticated by design — the secret is the credential. The cookie max-age is
    capped at the link TTL (never the jcode 30-day default), so it cannot outlive the
    link's box even in the browser; the principal carries the same expiry server-side."""
    result = await service.redeem_intake_link(repo, auth_repo, body.secret)
    if result is None:
        raise HTTPException(status_code=401, detail="invalid, expired, or exhausted link")
    remaining = int((result.expires_at - datetime.now(UTC)).total_seconds())
    response.set_cookie(
        settings.session_cookie,
        result.cookie_token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=max(remaining, 0),
    )
    snap = result.config_snapshot
    return RedeemOut(
        session_id=result.session_id,
        link_id=result.link_id,
        opening_blurb=str(snap.get("opening_blurb", "")),
        capture_enterer_name=bool(snap.get("capture_enterer_name", True)),
        disclose_owner_identity=bool(snap.get("disclose_owner_identity", False)),
    )
