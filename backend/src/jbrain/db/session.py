"""RLS-scoped database sessions.

Every application query runs through `scoped_session`, which sets the
per-transaction GUCs that Postgres row-level-security policies read. This is
the enforcement point for domain firewalls: a session without a scope simply
cannot see firewalled rows, regardless of what SQL the application runs.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class SessionContext:
    """Identity and scopes a database session runs under."""

    principal_id: str = ""
    principal_kind: str = ""
    subject_id: str = ""
    domain_scopes: Sequence[str] = field(default_factory=tuple)
    # 'login' and 'bootstrap' unlock the narrow RLS policies that let the
    # auth code path read/write principals before a principal context exists.
    auth_context: str = ""
    # When True, the owner is *also* restricted to domain_scopes (migration 0015):
    # the firewall a narrowed agent session runs under, so a health-only session
    # cannot read finance even though it keeps owner identity for owner-only
    # tables. Off by default — the worker and ordinary owner sessions see all.
    owner_scoped: bool = False

    def gucs(self) -> dict[str, str]:
        return {
            "app.principal_id": self.principal_id,
            "app.principal_kind": self.principal_kind,
            "app.subject_id": self.subject_id,
            "app.domain_scopes": ",".join(self.domain_scopes),
            "app.auth_context": self.auth_context,
            "app.owner_scoped": "true" if self.owner_scoped else "false",
        }


class ScopeStampError(ValueError):
    """A job's (principal_id, domain_code) scope stamp is malformed — exactly one
    half present. Raised (never silently downgraded to a system context) so a
    partial stamp fails CLOSED: a confused deputy can never widen its scope by
    sending a half-stamp (E1 / I-8, docs/WORKFLOW_ENGINE_PLAN.md §2)."""


def narrowed_context(principal_id: str | None, domain_code: str | None) -> SessionContext:
    """The narrowed `SessionContext` a *stamped* job runs under, fail-closed.

    A complete stamp (both halves present) yields an owner-narrowed session
    (`owner_scoped=True`) firewalled to the single `domain_code`, carrying the
    triggering principal — the no-confused-deputy scope (E1): the job sees only the
    one domain its trigger touched, even though it keeps owner identity for
    owner-only tables.

    Fail-closed is the whole point. A *partial* stamp (one half present, the other
    NULL/empty) is an ERROR, not a silent widening to `SYSTEM_CTX`: a caller that
    sets a principal but drops the domain — or vice versa — must not thereby earn
    the all-domains system scope. A both-absent stamp is a *system* job, but that
    decision belongs to the worker (`SYSTEM_CTX`), not here: this helper is invoked
    only once the worker has decided a stamp is present, so a both-empty input is
    itself a bug and also raises.
    """
    has_principal = bool(principal_id)
    has_domain = bool(domain_code)
    if not (has_principal and has_domain):
        raise ScopeStampError(
            "incomplete job scope stamp:"
            f" principal_id={principal_id!r} domain_code={domain_code!r}"
            " (a stamp must carry BOTH a principal and a domain, or neither)"
        )
    return SessionContext(
        principal_id=principal_id or "",
        principal_kind="owner",
        domain_scopes=(domain_code or "",),
        owner_scoped=True,
    )


@asynccontextmanager
async def scoped_session(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext
) -> AsyncIterator[AsyncSession]:
    """Open a transaction with `ctx` applied via SET LOCAL, commit on exit."""
    async with maker() as session, session.begin():
        for key, value in ctx.gucs().items():
            # set_config with is_local=true scopes the GUC to this transaction.
            await session.execute(
                text("SELECT set_config(:key, :value, true)"),
                {"key": key, "value": value},
            )
        yield session
