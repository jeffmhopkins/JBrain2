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

    def gucs(self) -> dict[str, str]:
        return {
            "app.principal_id": self.principal_id,
            "app.principal_kind": self.principal_kind,
            "app.subject_id": self.subject_id,
            "app.domain_scopes": ",".join(self.domain_scopes),
            "app.auth_context": self.auth_context,
        }


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
