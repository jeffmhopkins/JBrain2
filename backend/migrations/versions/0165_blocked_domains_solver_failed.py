"""Admit a `solver_failed` reason into the app.blocked_domains CHECK.

The learned Tavily-first routing (docs/plans/TAVILY_FETCH_TIER_PLAN.md) records a domain when the
on-box solver (byparr) genuinely misses but the hosted Tavily tier recovers the page, so future
fetches skip straight to Tavily. It reuses the existing per-domain fetch-health store
(app.blocked_domains, 0163) with the SAME 24h lazy-TTL, but a new REASON — `solver_failed` — which
is a REROUTE, not a skip (it is excluded from `active_hosts()` and surfaced only via
`tavily_first_hosts()`). The column's inline CHECK must admit the new reason or the upsert would
fail outright. The constraint name is Postgres's default for a column check
(`{table}_{column}_check`).
"""

from alembic import op

revision = "0165"
down_revision = "0164"
branch_labels = None
depends_on = None

_REASONS_OLD = "('paywalled', 'bot_blocked', 'unreadable')"
_REASONS_NEW = "('paywalled', 'bot_blocked', 'unreadable', 'solver_failed')"


def _set_reason_check(reasons: str) -> None:
    op.execute("ALTER TABLE app.blocked_domains DROP CONSTRAINT blocked_domains_reason_check")
    op.execute(
        "ALTER TABLE app.blocked_domains ADD CONSTRAINT blocked_domains_reason_check "
        f"CHECK (reason IN {reasons})"
    )


def upgrade() -> None:
    _set_reason_check(_REASONS_NEW)


def downgrade() -> None:
    # A downgrade must not leave rows that violate the tightened CHECK: drop any solver_failed
    # marks first (they are ephemeral 24h routing hints, safe to discard), then re-narrow.
    op.execute("DELETE FROM app.blocked_domains WHERE reason = 'solver_failed'")
    _set_reason_check(_REASONS_OLD)
