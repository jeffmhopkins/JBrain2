"""`endpoint_settings` — the panel knobs a panel is allowed to read (ROOM_ENDPOINT_PLAN.md §10.4ab).

Microphone gain, speaker volume and display brightness were compile-time constants, so every
one of them cost a build, a CI run, a deploy and an OTA to change. The volume took two such
rounds to land on 70; the gain and the brightness have never been tuned at all, because the
loop is too slow to bother with. Brightness in particular is still `0xFF` and still wrong for
a bedroom, and quiet hours needs to change it at runtime by design.

**WHY THIS IS NOT `app.settings`.** That is where the remembered Wi-Fi lives and it would have
been the obvious home — but it is gated on a bare `app.is_owner()` and it holds the Gmail
client secret, the Moltbook bearer key, the autonomy switch and the global kill. A panel
authenticates as a `device_key`, and `device_context()` refuses on purpose to launder a device
into owner scope so that a stolen panel key cannot read everything. Putting panel knobs there
would have meant either laundering that scope or relying on "the route only returns three
fields" — and `0178_settings_deny_jmolt` exists precisely to argue that a route's shape is a
code-review convention, not a mechanism.

So: a separate table whose contents are, by construction, worth exactly nothing to a thief.
Three numbers about a panel's own output.

**The policy is split rather than a single `USING`.** The owner may do anything; a `device_key`
may only SELECT. A panel that could write its own volume would be a device on a child's wall
able to raise the level in its own ear, which is the one thing the 65 dB(A) reasoning in
§10.4q exists to prevent.

**One row, not one per panel.** Both units are the same product in the same house, and the
first thing a second row would buy is the chance for them to disagree. Per-panel overrides can
come when something actually needs them; `id` is fixed at 1 so there is nothing to choose.

**The ceilings live in the API, not here.** A CHECK constraint that rejects a bad write gives
a 500 and no guidance; clamping at the route turns a typo of `700` into a safe `85` and says
so. The column types stop nonsense, the route stops mistakes.

Revision ID: 0206
Revises: 0205
Create Date: 2026-09-21
"""

from alembic import op

revision = "0206"
down_revision = "0205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.endpoint_settings (
            id smallint PRIMARY KEY DEFAULT 1,
            volume smallint NOT NULL DEFAULT 70,
            mic_gain_db smallint NOT NULL DEFAULT 30,
            brightness smallint NOT NULL DEFAULT 255,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT endpoint_settings_singleton CHECK (id = 1)
        )
        """
    )
    # The defaults are the values the firmware already ships with, so a box that has never
    # been touched behaves exactly as it did before this table existed.
    op.execute("INSERT INTO app.endpoint_settings (id) VALUES (1)")

    op.execute("ALTER TABLE app.endpoint_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.endpoint_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY endpoint_settings_owner ON app.endpoint_settings
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # Read-only for a panel, and FOR SELECT is what makes it read-only: a policy with no
    # WITH CHECK cannot authorise a write at all, so the restriction is structural rather
    # than a matter of which routes happen to exist.
    op.execute(
        """
        CREATE POLICY endpoint_settings_panel_read ON app.endpoint_settings
        FOR SELECT
        USING (current_setting('app.principal_kind', true) = 'device_key')
        """
    )
    # RLS decides WHICH rows; the grant decides whether the app role may reach the table at
    # all. Without it every query is `permission denied` before a policy is consulted.
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.endpoint_settings TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.endpoint_settings")
