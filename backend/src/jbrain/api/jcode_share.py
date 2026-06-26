"""Share links for a jcode session: mint / list / revoke (owner) + redeem (public).

A share link lets the owner open ONE code-mode session on any browser (D2). Mint
returns a single-use-printed secret bound to the session with a TTL; the owner hands
out the link, and the recipient's browser POSTs the secret to /jcode/share/redeem,
which exchanges it for a session cookie scoped to that one session (the jcode access
gate). The owner can list live shares and revoke any of them; revocation and expiry
both fail the cookie closed on the next request.

The token is reused capability-token machinery (256-bit secret, SHA-256 hashed, a
`principals` row with expiry/revocation) — see jbrain.auth.service. Management is
owner-gated; redeem is intentionally unauthenticated (the secret IS the credential).
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 - Pydantic needs the runtime symbol

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import AuthRepoDep, OwnerDep, SettingsDep
from jbrain.auth import service

router = APIRouter()

# Same shape gate as the jcode REST surface (the share is bound to one session id).
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valid_sid(sid: str) -> None:
    if not _SID_RE.match(sid):
        raise HTTPException(status_code=404, detail="unknown session")


class MintShareRequest(BaseModel):
    label: str = Field(default="shared link", min_length=1, max_length=128)
    # 15 minutes to 30 days; a share link must be time-boxed (no never-expiring grant).
    ttl_hours: float = Field(default=24.0, ge=0.25, le=24 * 30)


class MintShareOut(BaseModel):
    id: str
    label: str
    expires_at: datetime | None
    # The bearer secret — shown EXACTLY once, never recoverable from the list.
    token: str


class ShareOut(BaseModel):
    id: str
    label: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class RedeemRequest(BaseModel):
    token: str = Field(min_length=1)


class RedeemOut(BaseModel):
    session_id: str


@router.post("/jcode/sessions/{sid}/share", status_code=201)
async def mint_share(
    sid: str, body: MintShareRequest, _owner: OwnerDep, repo: AuthRepoDep
) -> MintShareOut:
    """Mint a time-boxed share link for a session (owner only). Returns the secret once.

    Residual the recipient inherits (accepted; see docs/proposed/JCODE_PLAN.md "Security
    posture"): a share grants access to ONE session, but the sandbox runs every session in
    one root container, so a recipient's shell/agent can read OTHER sessions' checkouts and
    has unrestricted NAT egress. Acceptable for a single-owner box handing time-boxed,
    revocable links over code-only data; do not widen sharing to untrusted users without
    the per-session FS guard + default-deny egress landing first.
    """
    _valid_sid(sid)
    token, record = await service.mint_jcode_share(repo, sid, body.label, body.ttl_hours)
    return MintShareOut(id=record.id, label=record.label, expires_at=record.expires_at, token=token)


@router.get("/jcode/sessions/{sid}/shares")
async def list_shares(sid: str, _owner: OwnerDep, repo: AuthRepoDep) -> list[ShareOut]:
    """The live (non-revoked) share links for a session — metadata only, no secrets."""
    _valid_sid(sid)
    shares = await repo.list_jcode_shares(sid)
    return [
        ShareOut(
            id=s.id,
            label=s.label,
            created_at=s.created_at,
            expires_at=s.expires_at,
            last_used_at=s.last_used_at,
        )
        for s in shares
    ]


@router.delete("/jcode/sessions/{sid}/shares/{share_id}", status_code=204)
async def revoke_share(sid: str, share_id: str, _owner: OwnerDep, repo: AuthRepoDep) -> None:
    """Revoke a share link (owner only). 404 on an unknown / already-revoked / wrong-session id."""
    _valid_sid(sid)
    if not await repo.revoke_jcode_share(share_id, sid):
        raise HTTPException(status_code=404, detail="unknown share link")


@router.post("/jcode/share/redeem")
async def redeem_share(
    body: RedeemRequest,
    response: Response,
    request: Request,
    repo: AuthRepoDep,
    settings: SettingsDep,
) -> RedeemOut:
    """Exchange a share secret for a session cookie scoped to that one session. 401 on an
    invalid / revoked / lapsed secret, writing no cookie. Unauthenticated by design — the
    secret is the credential.

    An OWNER opening their own share link is NOT downgraded: if the request already
    carries a live owner session, we return the session id WITHOUT clobbering that cookie
    (the owner already has full access). Only a non-owner browser gets the scoped cookie —
    so the worst a forced redeem can do to a victim is a downgrade to a single sandbox,
    never a privilege change."""
    share = await service.validate_jcode_share(repo, body.token)
    if share is None:
        raise HTTPException(status_code=401, detail="invalid or expired share link")
    existing = await service.authenticate(repo, request.cookies.get(settings.session_cookie, ""))
    if existing is not None and existing.kind == "owner":
        return RedeemOut(session_id=share.jcode_session_id)
    redeemed = await service.redeem_jcode_share(repo, body.token)
    if redeemed is None:  # the secret was just validated; this satisfies the type checker
        raise HTTPException(status_code=401, detail="invalid or expired share link")
    token, session_id = redeemed
    response.set_cookie(
        settings.session_cookie,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return RedeemOut(session_id=session_id)
