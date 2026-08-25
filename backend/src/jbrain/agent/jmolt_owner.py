"""The one owner principal jmolt's singleton data is anchored to.

The owner key rotates over a box's life, and each rotation REVOKES the old owner
principal and mints a NEW one (auth). So a long-lived box carries several `owner`
rows in `app.principals` — at most one un-revoked — and a live box observed three
(two revoked, one active).

jmolt's cross-night data — its scratchpad, journal, outbox, and action ledger — is a
SINGLETON keyed by `principal_id`. Every jmolt writer (the night, the drip sweep, the
integrity watch) AND every reader of that data (the morning digest, jerv's observation,
the PWA history browser) must resolve the SAME principal, or the data splits across
principals or vanishes from a view. The bug this fixes: the writers resolved
`WHERE kind='owner' LIMIT 1` (unordered → the OLDEST row) while the PWA read endpoints
filtered by the AUTHENTICATED owner (the newest, active row after two rotations), so
jmolt's real files showed as an empty notebook.

We anchor to the **oldest** owner principal, deterministically:

- It is STABLE. Principals are revoked, never deleted, so the oldest row never changes —
  the anchor holds for the life of the box and no data ever needs re-homing when a key
  rotates (following the *active* owner instead would strand jmolt's history on the
  previous principal at every rotation).
- Auth does not depend on it. jmolt's writes are gated by `auth_ctx()='jmolt'` (+ the
  principal-pinned WITH CHECK), and every owner-side read by `is_owner()`; neither checks
  that this anchor principal is un-revoked. So a revoked row is a perfectly good stable
  home — this id is jmolt's filing key, not a credential.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from jbrain.db.session import SessionContext, scoped_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SYSTEM_OWNER = SessionContext(principal_kind="owner")


async def jmolt_owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    """The stable owner principal jmolt's data is filed under — the OLDEST `owner` row.
    Deterministic (`ORDER BY created_at`), so every jmolt writer and reader agrees on it
    regardless of how many key rotations the box has seen. None on a box with no owner yet."""
    sql = "SELECT id FROM app.principals WHERE kind = 'owner' ORDER BY created_at LIMIT 1"
    async with scoped_session(maker, _SYSTEM_OWNER) as session:
        pid = (await session.execute(text(sql))).scalar()
    return str(pid) if pid is not None else None
