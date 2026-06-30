"""Mint / redeem / revoke flows for guided-intake share links.

The repository protocol keeps these security-critical flows unit-testable with a
fake; `SqlIntakeRepo` is exercised against real Postgres in the integration suite,
where the RLS firewall (which a fake cannot model) is the load-bearing proof.

Mint and revoke run under the OWNER's RLS context (full owner — never the
`is_owner()` shortcut). Redeem runs before any principal exists: the repo's atomic
`claim` reads the link by secret and binds a per-session non-owner principal under
the `bootstrap` auth context, then this layer mints the session cookie bound to
that principal (mirroring `mint_dashboard_session` for a device key).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from jbrain.auth import keys
from jbrain.auth.service import AuthRepo
from jbrain.db.session import SessionContext


@dataclass(frozen=True)
class IntakeLinkConfig:
    """The owner's mint parameters for a link (the agent-staged defaults, W4)."""

    subject_id: str
    domain_code: str
    label: str
    persona_brief: str
    fields_brief: str
    opening_blurb: str
    max_runs: int
    max_opens: int
    bind_on_first: bool
    ttl_hours: float
    capture_enterer_name: bool = True
    disclose_owner_identity: bool = False


@dataclass(frozen=True)
class IntakeLinkRecord:
    """A link as the owner's management list sees it — carries NO secret (#14)."""

    id: str
    subject_id: str
    domain_code: str
    label: str
    persona_brief: str
    fields_brief: str
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


@dataclass(frozen=True)
class IntakeSessionRecord:
    """One opened session (the owner's per-link conversation browse, #15)."""

    id: str
    link_id: str
    principal_id: str
    opened_at: datetime
    status: str
    config_snapshot: dict


@dataclass(frozen=True)
class IntakeSubmissionRecord:
    """A captured submission as the owner reviews it. `transcript` is populated only
    by the per-submission deep read (`get_submission`), not the list (#15)."""

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
    # Filled only by the per-submission deep read; None in the list view (#15).
    transcript: list | None = None


@dataclass(frozen=True)
class ClaimResult:
    """The output of the atomic redeem claim: a freshly bound per-session principal,
    its session row, and the link's box (the cookie cannot outlive `expires_at`)."""

    principal_id: str
    session_id: str
    link_id: str
    config_snapshot: dict
    expires_at: datetime


@dataclass(frozen=True)
class RedeemResult:
    """A successful redeem: the session cookie + the session context the recipient
    surface needs. `expires_at` caps the cookie max-age at the link TTL."""

    session_id: str
    link_id: str
    cookie_token: str
    config_snapshot: dict
    expires_at: datetime


class IntakeRepo(Protocol):
    async def create_link(
        self, ctx: SessionContext, *, secret_hash: str, config: IntakeLinkConfig
    ) -> IntakeLinkRecord: ...

    async def list_links(self, ctx: SessionContext) -> list[IntakeLinkRecord]: ...

    async def get_link(self, ctx: SessionContext, link_id: str) -> IntakeLinkRecord | None: ...

    async def revoke_link(self, ctx: SessionContext, link_id: str) -> bool: ...

    async def list_sessions(
        self, ctx: SessionContext, link_id: str
    ) -> list[IntakeSessionRecord]: ...

    async def list_submissions(
        self, ctx: SessionContext, link_id: str
    ) -> list[IntakeSubmissionRecord]: ...

    async def get_submission(
        self, ctx: SessionContext, submission_id: str
    ) -> IntakeSubmissionRecord | None: ...

    async def claim(
        self, *, secret_hash: str, principal_key_hash: str, label: str
    ) -> ClaimResult | None: ...


async def mint_intake_link(
    repo: IntakeRepo, ctx: SessionContext, config: IntakeLinkConfig
) -> tuple[str, IntakeLinkRecord]:
    """Mint a link under the owner context; returns the secret EXACTLY once alongside
    its management record. The secret is a 256-bit URL-safe token stored only as its
    SHA-256 (#14) — to re-send a link, re-mint. The caller validates caps/TTL ranges."""
    secret = keys.generate_capability_key()
    record = await repo.create_link(ctx, secret_hash=keys.hash_token(secret), config=config)
    return secret, record


async def redeem_intake_link(
    repo: IntakeRepo, auth_repo: AuthRepo, secret: str
) -> RedeemResult | None:
    """Exchange a link secret for a session cookie scoped to a fresh non-owner
    principal, or None on an invalid / revoked / lapsed / capped secret.

    The atomic `claim` is the gate: opens burn at redeem under one conditional UPDATE,
    so concurrent redeems can never exceed `max_opens` (or 1, bind-on-first). Only on a
    win is a cookie minted — bound to the per-session principal, which carries the
    link's expiry so the cookie fails closed server-side at TTL, not just in the
    browser. Consuming an open does not revoke the principal; revoking the LINK does
    (it cascades to in-flight session principals)."""
    if not secret:
        return None
    cookie = keys.generate_session_token()
    claim = await repo.claim(
        secret_hash=keys.hash_token(secret),
        principal_key_hash=keys.hash_token(keys.generate_session_token()),
        label="intake",
    )
    if claim is None:
        return None
    # The cookie is minted in a SECOND transaction (DeviceSession is the auth domain's).
    # If it failed, the open is already burned and an unbound session row exists — an
    # acceptable held slot (§5 abandoned-session handling), never an over-issue or a leak.
    await auth_repo.create_session(claim.principal_id, keys.hash_token(cookie), "intake")
    return RedeemResult(
        session_id=claim.session_id,
        link_id=claim.link_id,
        cookie_token=cookie,
        config_snapshot=claim.config_snapshot,
        expires_at=claim.expires_at,
    )


async def revoke_intake_link(repo: IntakeRepo, ctx: SessionContext, link_id: str) -> bool:
    """Revoke a link (owner only): flip it to `revoked` AND kill any in-flight session
    cookies (the cascade to per-session principals). No-op (False) on an unknown /
    already-revoked id."""
    return await repo.revoke_link(ctx, link_id)
