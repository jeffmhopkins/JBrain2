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
import uuid
from datetime import datetime  # noqa: TC003 - Pydantic needs the runtime symbol
from typing import cast

import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.client_ip import device_key
from jbrain.api.deps import AuthRepoDep, OwnerDep, SettingsDep
from jbrain.api.session_cookie import session_token, set_session_cookie
from jbrain.api.ws_reauth import kick
from jbrain.auth import service
from jbrain.auth.service import AuthRepo
from jbrain.jcode.client import JcodeApi, JcodeError

router = APIRouter()
log = structlog.get_logger()

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
    # When the link was claimed (single-use); None = still open. Lets the owner's list
    # show "opened" vs "unused" so a stale, never-claimed link is easy to spot.
    redeemed_at: datetime | None


class RedeemRequest(BaseModel):
    token: str = Field(min_length=1, max_length=1024)  # unauthenticated, so bounded


class RedeemOut(BaseModel):
    session_id: str


@router.post("/jcode/sessions/{sid}/share", status_code=201)
async def mint_share(
    sid: str, body: MintShareRequest, _owner: OwnerDep, repo: AuthRepoDep
) -> MintShareOut:
    """Mint a time-boxed share link for a session (owner only). Returns the secret once.

    Residual the recipient inherits (accepted; see docs/archive/JCODE_PLAN.md "Security
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
            redeemed_at=s.redeemed_at,
        )
        for s in shares
    ]


@router.delete("/jcode/sessions/{sid}/shares/{share_id}", status_code=204)
async def revoke_share(
    sid: str, share_id: str, request: Request, _owner: OwnerDep, repo: AuthRepoDep
) -> None:
    """Revoke a share link (owner only). 404 on an unknown / already-revoked / wrong-session id.

    The share's open sockets (its terminal) close now, and the session's preview slug is
    rotated so a preview URL the recipient kept stops resolving; the owner's PWA re-reads
    the preview status after a revoke, so its own pane moves to the new URL. The slug is
    the session's, not the share's, so every OTHER live share of the session gets the new
    URL too, the next time its browser reads the preview status.
    The session's processes are left running: owner and recipient type into one
    reattached shell, so the recipient's can't be told from the owner's (security plan
    S3.1).

    Residual: a failed rotation (the control server unreachable) is logged and the revoke
    still stands, but the old preview URL stays valid until the session is rotated again,
    deleted or reaped."""
    _valid_sid(sid)
    try:
        # Canonical form, so the socket registry (keyed by principal id) is hit however
        # the caller spelled the uuid.
        share_id = str(uuid.UUID(share_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown share link") from None
    if not await revoke_link(request.app, repo, sid, share_id):
        raise HTTPException(status_code=404, detail="unknown share link")


async def revoke_link(app: FastAPI, repo: AuthRepo, sid: str, share_id: str) -> bool:
    """Revoke one share link and everything it opened: the row, the recipient's sockets, and
    the session's preview slug. Shared with JShare's all-links list, so a revoke there does
    exactly what one here does. False when no live link of `sid` has that id."""
    if not await repo.revoke_jcode_share(share_id, sid):
        return False
    kick(app, share_id)
    client = cast("JcodeApi | None", getattr(app.state, "jcode_client", None))
    if client is None:
        return True
    try:
        await client.preview_rotate(sid)
    except JcodeError as exc:
        # The revoke itself stands; the failure is logged for the debug console. (A session
        # the control server already dropped has no slug left to rotate.)
        log.warning("jcode.share_revoke_rotate_failed", sid=sid, error=str(exc))
    return True


@router.post("/jcode/share/redeem")
async def redeem_share(
    body: RedeemRequest,
    response: Response,
    request: Request,
    repo: AuthRepoDep,
    settings: SettingsDep,
) -> RedeemOut:
    """Exchange a share secret for a session cookie scoped to that one session. 401 on an
    invalid / revoked / lapsed / already-claimed secret, writing no cookie. Unauthenticated
    by design — the secret is the credential.

    SINGLE USE: the first browser to redeem CONSUMES the link and binds it; a later
    redeem (a forwarded copy, a second device) 401s. Two requests are idempotent and do
    NOT consume the link, so they never lock anyone out of access they already have:
    - an OWNER opening their own link (they have full access anyway), and
    - the already-bound browser reopening it (its cookie already grants this session).
    Both just get the session id back, no new cookie. So the worst a forced redeem can do
    to a victim is a downgrade to a single sandbox, never a privilege change."""
    share = await service.validate_jcode_share(repo, body.token)
    if share is None:
        raise HTTPException(status_code=401, detail="invalid or expired share link")
    existing = await service.authenticate(
        repo, session_token(request, settings), device=device_key(request)
    )
    bound_here = (
        existing is not None
        and existing.kind == "jcode_share_link"
        and existing.jcode_session_id == share.jcode_session_id
    )
    if existing is not None and (existing.kind == "owner" or bound_here):
        return RedeemOut(session_id=share.jcode_session_id)
    redeemed = await service.redeem_jcode_share(repo, body.token)
    if redeemed is None:  # validated above, so None here means another browser just claimed it
        raise HTTPException(status_code=401, detail="this share link has already been opened")
    token, session_id = redeemed
    set_session_cookie(response, settings, token, samesite="lax", max_age=60 * 60 * 24 * 30)
    return RedeemOut(session_id=session_id)
