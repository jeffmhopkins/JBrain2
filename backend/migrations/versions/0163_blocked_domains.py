"""The blocked_domains reference table — a 24h paywall/bot-wall skip list.

When `web_fetch` hits a PERSISTENT hard block on a domain (a paywall/subscriber
wall, a bot-challenge interstitial, or an unreadable JS shell), the DOMAIN is
recorded here for 24 hours: later web SEARCHES drop that host from their results
(telling the agent how many were hidden), and later FETCHES of it short-circuit
without a network call. Only persistent blocks are recorded — never a 404/410
(the page is gone, not the site), never a transient timeout/DNS/5xx.

Global SYSTEM reference data, like app.canonical_predicates (0031): every
principal reads it (a search runs under whatever scope the caller holds), but it
is self-extending, so writes are gated to the owner/system context. Expiry is
lazy — `expires_at > now()` is the source of truth, so a stale row simply stops
matching; no sweep is required.

Revision ID: 0163
Revises: 0162
Create Date: 2026-08-09
"""

from alembic import op

revision = "0163"
down_revision = "0162"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.blocked_domains (
            host text PRIMARY KEY,
            reason text NOT NULL
                CHECK (reason IN ('paywalled', 'bot_blocked', 'unreadable')),
            observed_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
            hit_count integer NOT NULL DEFAULT 1,
            last_url text
        )
        """
    )
    # Lazy expiry reads `expires_at > now()`; the index keeps that scan cheap.
    op.execute("CREATE INDEX ix_blocked_domains_expires_at ON app.blocked_domains (expires_at)")

    op.execute("ALTER TABLE app.blocked_domains ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.blocked_domains FORCE ROW LEVEL SECURITY")
    # Global reference data: every principal reads (app.canonical_predicates precedent, 0031).
    op.execute("CREATE POLICY blocked_domains_read ON app.blocked_domains FOR SELECT USING (true)")
    # Self-extending: only the owner/system context records a block or bumps a hit count.
    op.execute(
        "CREATE POLICY blocked_domains_insert ON app.blocked_domains"
        " FOR INSERT WITH CHECK (app.is_owner())"
    )
    op.execute(
        "CREATE POLICY blocked_domains_update ON app.blocked_domains"
        " FOR UPDATE USING (app.is_owner()) WITH CHECK (app.is_owner())"
    )
    # SELECT for every reader; INSERT/UPDATE record blocks and bump hit counts (the upsert).
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.blocked_domains TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.blocked_domains")
