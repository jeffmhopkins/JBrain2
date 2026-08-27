"""Deny jmolt's own auth context every row of app.settings (B9).

jmolt's nightly session runs as `principal_kind='owner'` — it has to, because the
scratchpad, outbox and ledger are all owner-principal rows. `app.settings` is gated on a
bare `app.is_owner()`, so the same session that reads a Moltbook thread written by a
stranger is, in Postgres' view, entitled to every settings row: the Moltbook bearer key,
the Gmail client secret, the autonomy switch, the global kill.

Nothing in jmolt's tool catalog exposes generic settings access today, so this is latent —
but "no tool does that" is a code-review convention, not a mechanism, and it is the
convention that has to hold across every wave that adds a tool. The strongest case is not
even the read. `moltbook_advisory_note` lives in this table and is injected into the one
channel the persona is told is genuinely from its human; a settings WRITE reachable from
jmolt's context is a self-instruction loop into the channel the design asserts cannot be
spoofed.

The nightly run's own settings work (the night deadline, the box hold) runs under the
OWNER context, not the jmolt one — `jmolt_run_context` is used for jmolt's own tables —
so denying `auth_ctx() = 'jmolt'` costs the night nothing.

Revision ID: 0178
Revises: 0177
Create Date: 2026-08-27
"""

from alembic import op

revision = "0178"
down_revision = "0177"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # RESTRICTIVE: it ANDs with the permissive owner policy rather than widening anything,
    # so this can only ever remove access. A permissive policy here would have granted the
    # complement instead — the opposite of the intent.
    op.execute(
        """
        CREATE POLICY settings_not_jmolt ON app.settings
        AS RESTRICTIVE
        USING (app.auth_ctx() IS DISTINCT FROM 'jmolt')
        WITH CHECK (app.auth_ctx() IS DISTINCT FROM 'jmolt')
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY settings_not_jmolt ON app.settings")
