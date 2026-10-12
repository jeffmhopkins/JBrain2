"""The owner principal that server-side "act as the owner" code runs as.

A box can carry several `kind='owner'` rows: every owner-key rotation revokes the old row and
mints a new one (`auth.service.rotate_owner_key`), and principals are revoked, never deleted.
The PWA's logins can only ever be the LIVE one — `login` matches `revoked_at IS NULL`, and a
session cookie stops authenticating the moment its principal is revoked — so a headless turn
that must land where the owner's PWA reads it has to resolve that same row.

`WHERE kind = 'owner' LIMIT 1` (no ORDER BY) did not: on a box with an older row it returned
whichever row the heap scan met first, which is usually the oldest — a revoked one. The desk
panel's "Desk panel" chats were filed under it (2026-10-12), and with them every lookup keyed
by principal (the owner's standing instructions, `prefs_read`) read a stale principal's rows.

Picked, in order: un-revoked first; then the row the owner's PWA has most recently been seen
on (`device_sessions.last_seen_at` of a live session), which settles even the abnormal case of
two un-revoked owner rows by "the one his logins actually use"; then the newest. Deterministic,
and never "the newest owner" alone — a fresh bootstrap row the owner never logged in with does
not win over the one he uses.

jmolt deliberately anchors to the OLDEST owner row instead (`agent.jmolt_owner`): its data is a
filing key that must never move on a rotation. That is a different question with a different
answer, and is not this function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from jbrain.db.session import SessionContext, scoped_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Owner identity is what `is_owner()` checks; this context reads `principals` and
# `device_sessions` (both owner-readable) and no domain data.
_SYSTEM_OWNER = SessionContext(principal_kind="owner")

#: Also run by migration 0234 to re-home the panel's misfiled threads, so the repair and the
#: runtime can never disagree about who the owner is.
OWNER_PRINCIPAL_SQL = """
    SELECT p.id FROM app.principals p
    WHERE p.kind = 'owner'
    ORDER BY (p.revoked_at IS NULL) DESC,
             (SELECT max(ds.last_seen_at) FROM app.device_sessions ds
              WHERE ds.principal_id = p.id AND ds.revoked_at IS NULL) DESC NULLS LAST,
             p.created_at DESC, p.id
    LIMIT 1
"""


async def owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    """The owner principal the PWA's logins use, as a string. None on a box with no owner."""
    async with scoped_session(maker, _SYSTEM_OWNER) as session:
        found = (await session.execute(text(OWNER_PRINCIPAL_SQL))).scalar()
    return None if found is None else str(found)
