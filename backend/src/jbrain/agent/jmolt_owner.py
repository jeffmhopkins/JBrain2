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


def jmolt_settings_ctx(session: SessionContext) -> SessionContext:
    """A NON-jmolt owner context for the settings reads jmolt's tools legitimately make.

    `app.settings` denies `auth_context='jmolt'` outright (migration 0178, B9). jmolt runs as
    the owner principal, so without that policy the same session that just read a stranger's
    post is entitled to every settings row: the Moltbook bearer key, the Gmail client secret,
    the kill switch, and `moltbook_advisory_note` — which is injected into the one channel the
    persona is told is genuinely from its human, making a settings write from jmolt's context
    a self-instruction loop into the channel the design asserts cannot be spoofed.

    Three tool handlers still need values from that table (the release switch, the disclosure
    line, the night deadline and timezone), so they drop the jmolt auth context here. That is
    not a hole the model can reach — it cannot choose which context a handler queries under.
    What the policy buys is that a settings read has to be WRITTEN, deliberately, at a named
    call site: a generic settings tool added in a later wave is refused by Postgres rather
    than by someone remembering the convention.

    Same shape as `jmolt_sweep._admin_ctx`, which the drip has always used for exactly this.
    """
    return SessionContext(
        principal_id=session.principal_id,
        principal_kind="owner",
        domain_scopes=("jmolt",),
    )
