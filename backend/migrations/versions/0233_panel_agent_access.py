"""The owner's desk panel reaches the real agents: a per-panel allow, and its threads.

docs/plans/JPANEL_C6_PLAN.md, owner decision 2026-10-12: *"it should be the full jerv with full
tool access. I'm explicitly permitting this."* Until now `POST /endpoint/converse?persona=jerv`
answered with a tool-less persona, because a device key that reaches the agent is worth the
owner's whole agent rather than "one conversation". The owner has accepted that for his own
panel, so the question becomes which device keys it is true for — and the answer must not be
anything a device can say about itself.

- **`endpoint_panel.agent_access`**, default FALSE. The owner sets it (PWA Panels screen, or
  the debug API); a panel can only read its own row (`endpoint_panel_own`) and can write none,
  so no request a panel makes can turn it on. The route also requires `subjects.device_role =
  'display'`, which only the owner's flash writes: a twin's `jpet` panel never reaches the
  agent even if this flag were somehow set on its row.
- **`endpoint_panel_thread`**: which agent session a panel's turns land in, one per (panel,
  agent) — the owner's Jerv history and his Minecraft_Dave history each gain one rolling
  "Desk panel" thread. Owner-only: the turn runs as the owner, and a panel has no reason to
  read which session it is. Cascades with the session, so a thread the owner deletes in the PWA
  simply starts a fresh one on the next turn.

**The seed is narrow on purpose.** The owner's own panel is the one `display` panel named
`Jeff` (`panel Jeff`). It is switched on only when exactly one live subject matches; on a box
with none, or more than one, nothing changes and the owner sets it from the PWA.

Revision ID: 0233
Revises: 0232
Create Date: 2026-10-12
"""

from alembic import op

revision = "0233"
down_revision = "0232"
branch_labels = None
depends_on = None

# Switches the agents on for the owner's own panel, and only when it is unambiguous. Named so
# the integration suite runs the same statement against a box that has such a panel.
SEED_OWNER_PANEL = """
        WITH jeff AS (
            SELECT s.id FROM app.subjects s
            WHERE s.kind = 'device' AND s.device_role = 'display'
              AND s.display_name = 'panel Jeff'
              AND EXISTS (
                  SELECT 1 FROM app.principals p
                  WHERE p.subject_id = s.id AND p.kind = 'device_key' AND p.revoked_at IS NULL
              )
        )
        INSERT INTO app.endpoint_panel (subject_id, agent_access)
        SELECT id, true FROM jeff WHERE (SELECT count(*) FROM jeff) = 1
        ON CONFLICT (subject_id) DO UPDATE SET agent_access = true, updated_at = now()
"""


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.endpoint_panel ADD COLUMN agent_access boolean NOT NULL DEFAULT false"
    )
    op.execute(
        """
        CREATE TABLE app.endpoint_panel_thread (
            subject_id uuid NOT NULL REFERENCES app.subjects(id) ON DELETE CASCADE,
            agent text NOT NULL CHECK (agent IN ('jerv', 'minecraft_dave')),
            agent_session_id uuid NOT NULL
                REFERENCES app.agent_sessions(id) ON DELETE CASCADE,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (subject_id, agent)
        )
        """
    )
    op.execute("ALTER TABLE app.endpoint_panel_thread ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.endpoint_panel_thread FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY endpoint_panel_thread_owner ON app.endpoint_panel_thread
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.endpoint_panel_thread TO jbrain_app")

    op.execute(SEED_OWNER_PANEL)


def downgrade() -> None:
    op.execute("DROP TABLE app.endpoint_panel_thread")
    op.execute("ALTER TABLE app.endpoint_panel DROP COLUMN agent_access")
