"""The desk panel's threads, filed under the owner principal his PWA actually uses.

0.0.11's panel turns ran as `WHERE kind='owner' LIMIT 1` — no ORDER BY — and a box whose owner
key has been rotated carries more than one owner row (a rotation revokes the old row and mints a
new one). On the live box that query met the older, revoked row first, so the "Desk panel"
threads (and anything a panel turn staged) were filed under a principal the PWA no longer logs
in as, while every PWA chat belonged to the live one (2026-10-12).

This re-homes them: every session an `endpoint_panel_thread` row points at, its sub-agent
children, and the Proposals staged in them, move to the owner principal `jbrain.auth.owner`
resolves — un-revoked first, then the one with the most recently seen live PWA session, then
the newest. Idempotent (a second run matches nothing), and a box with one owner row is
untouched. The runtime does the same on every panel turn (`panel_agent._rehome`), so a later
rotation needs no migration of its own.

No new table, so no new RLS surface. Revision 0233's tables keep their owner-only policies.

Revision ID: 0234
Revises: 0233
Create Date: 2026-10-12
"""

from alembic import op

revision = "0234"
down_revision = "0233"
branch_labels = None
depends_on = None

#: Must pick the same row as `jbrain.auth.owner.OWNER_PRINCIPAL_SQL` (the integration suite runs
#: both against a box with two owner rows and compares).
LIVE_OWNER = """
    SELECT p.id FROM app.principals p
    WHERE p.kind = 'owner'
    ORDER BY (p.revoked_at IS NULL) DESC,
             (SELECT max(ds.last_seen_at) FROM app.device_sessions ds
              WHERE ds.principal_id = p.id AND ds.revoked_at IS NULL) DESC NULLS LAST,
             p.created_at DESC, p.id
    LIMIT 1
"""

REHOME = f"""
    WITH owner AS ({LIVE_OWNER}),
    threads AS (
        SELECT s.id FROM app.agent_sessions s
        WHERE s.id IN (SELECT agent_session_id FROM app.endpoint_panel_thread)
           OR s.parent_session_id IN (SELECT agent_session_id FROM app.endpoint_panel_thread)
    ),
    moved AS (
        UPDATE app.agent_sessions SET principal_id = (SELECT id FROM owner)
        WHERE id IN (SELECT id FROM threads)
          AND principal_id <> (SELECT id FROM owner)
        RETURNING id
    )
    UPDATE app.proposals SET principal_id = (SELECT id FROM owner)
    WHERE session_id IN (SELECT id FROM threads)
      AND principal_id <> (SELECT id FROM owner)
"""


def upgrade() -> None:
    op.execute(REHOME)


def downgrade() -> None:
    # Which principal a thread was misfiled under is not worth keeping: nothing to undo.
    pass
