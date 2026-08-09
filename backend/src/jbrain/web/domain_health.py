"""The 24h paywall/bot-wall skip list (docs/plans/DOMAIN_HEALTH_PLAN.md).

`DomainSkipRepo` is the read/write seam over `app.blocked_domains` (migration
0163): the web fetcher records a DOMAIN when it hits a persistent hard block
(a paywall, a bot-challenge wall, an unreadable shell), and the web tools consult
the active set to short-circuit a fetch of a listed host and drop listed hosts
from search results.

Both methods run under `queue.SYSTEM_CTX` (an owner-kind context) because this is
global SYSTEM reference data, not owner-domain data — the RLS write-gate is
`app.is_owner()` (like app.canonical_predicates). Expiry is lazy: `active_hosts`
filters on `expires_at > now()`, so a stale row simply stops matching and no sweep
is needed. Both methods are FAIL-OPEN and BEST-EFFORT — a DB hiccup must never
sink a fetch or a search, so `record` swallows its errors and `active_hosts`
returns an empty set on any error (better to show a paywalled hit than to break
search on a DB blip).
"""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX
from jbrain.web.favicon import normalize_host

log = structlog.get_logger()

# The reasons a domain lands on the skip list — the CHECK constraint on the table.
VALID_REASONS = frozenset({"paywalled", "bot_blocked", "unreadable"})


class DomainSkipRepo:
    """CRUD for the 24h blocked-domain skip list on a SYSTEM-scoped session. Holds the
    async_sessionmaker and writes/reads under `SYSTEM_CTX` — the owner context the write
    policy requires — mirroring the worker repos (jbrain.queue, jbrain.embed)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def record(self, host: str, reason: str, url: str) -> None:
        """Record (or refresh) a persistent block on `host`. Upserts the row: a repeat block
        bumps `hit_count`, resets the 24h window, and updates the reason + last URL. The host
        is normalized to a bare lowercase name (`normalize_host`) so it keys one row per site,
        and matches the key `active_hosts` returns. Best-effort — a bad reason or any DB error
        is swallowed and logged, never raised into the fetch handler."""
        key = normalize_host(host) or normalize_host(url)
        if key is None or reason not in VALID_REASONS:
            return
        try:
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO app.blocked_domains (host, reason, last_url)
                        VALUES (:host, :reason, :url)
                        ON CONFLICT (host) DO UPDATE SET
                            reason = EXCLUDED.reason,
                            last_url = EXCLUDED.last_url,
                            observed_at = now(),
                            expires_at = now() + interval '24 hours',
                            hit_count = app.blocked_domains.hit_count + 1
                        """
                    ),
                    {"host": key, "reason": reason, "url": url},
                )
        except Exception:  # noqa: BLE001 - recording a block must never break the fetch
            log.warning("domain_skip.record_failed", host=key, reason=reason, exc_info=True)

    async def active_hosts(self) -> frozenset[str]:
        """The bare lowercase hosts whose block is still live (`expires_at > now()`), read under
        SYSTEM_CTX. Lazy expiry is the source of truth, so a past-TTL row is simply not returned.
        FAIL-OPEN: any DB error yields an empty set, so a hiccup never blocks a search or fetch."""
        try:
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                rows = (
                    (
                        await session.execute(
                            text("SELECT host FROM app.blocked_domains WHERE expires_at > now()")
                        )
                    )
                    .scalars()
                    .all()
                )
            return frozenset(h.lower() for h in rows if h)
        except Exception:  # noqa: BLE001 - never break search on a DB blip; fail open
            log.warning("domain_skip.active_hosts_failed", exc_info=True)
            return frozenset()
