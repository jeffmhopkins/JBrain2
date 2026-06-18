"""View-scope membership lookup for the live (MQTT) path.

The history path enforces family-sees-family inside the `location_fixes` RLS policy
(migration 0067). The live path is its twin: the MQTT ACL endpoint asks "may this
device subscribe to that group member's topic?" — i.e. "do these two subjects share
a family group?" — which is exactly `app.viewer_may_see`. That function is SECURITY
DEFINER, so it answers regardless of the caller's scope; a bare session suffices to
invoke it.
"""

from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

# viewer_may_see reads the owner-only view_scope internally (as definer), so the
# caller needs no domain scope — only EXECUTE, which PUBLIC has.
_CTX = SessionContext()


class ViewScopeRepo(Protocol):
    async def may_view(self, viewer_subject_id: str, target_subject_id: str) -> bool: ...


class SqlViewScopeRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def may_view(self, viewer_subject_id: str, target_subject_id: str) -> bool:
        """True iff the two subjects share a family group (deny-by-default)."""
        if not viewer_subject_id or not target_subject_id:
            return False
        async with scoped_session(self._maker, _CTX) as session:
            allowed = (
                await session.execute(
                    text("SELECT app.viewer_may_see(:v, :t)"),
                    {"v": viewer_subject_id, "t": target_subject_id},
                )
            ).scalar()
        return bool(allowed)
