"""`panel_notice` gains a final state, `withdrawn`: the owner took a notice back.

docs/plans/JPANEL_C6_PLAN.md §2 "The owner's composer" (owner, 2026-10-14: an Unsend button
while a notice is queued, delivered or shown). The owner moves an open notice to `withdrawn`;
a queued one is then never delivered, and a panel that already holds one learns it on its next
debug pickup (`X-Notice-Withdraw`) and takes it off the glass without a word back.

**The panel's read policy gains one arm** so the pickup can name, under the PANEL's own
context, which of the notices it collected were withdrawn: its own, `withdrawn`, delivered, and
still inside the answer window it was sent with (`expires_at` is left as it was, so that window
is exactly how long the panel could still be holding it). Past it the panel has dropped the
notice on its own and the row leaves its view again. A sibling still sees nothing of another
panel's, and the update policy is unchanged: no panel can move a withdrawn notice anywhere,
because its USING clause admits only the open states.

Revision ID: 0249
Revises: 0248
Create Date: 2026-10-14
"""

from alembic import op

revision = "0249"
down_revision = "0248"
branch_labels = None
depends_on = None

_PANEL_OWN = """
    current_setting('app.principal_kind', true) = 'device_key'
    AND device_id::text = current_setting('app.principal_id', true)
"""


def _read_policy(withdrawn_arm: bool) -> str:
    arm = (
        "OR (state = 'withdrawn' AND delivered_at IS NOT NULL AND expires_at > now())"
        if withdrawn_arm
        else ""
    )
    return f"""
        CREATE POLICY panel_notice_panel_read ON app.panel_notice
        FOR SELECT USING (
            {_PANEL_OWN}
            AND (
                (state IN ('queued', 'delivered', 'shown') AND expires_at > now())
                OR (state IN ('answered', 'dismissed', 'expired') AND finished_at = now())
                {arm}
            )
        )
    """


def upgrade() -> None:
    op.execute("ALTER TABLE app.panel_notice DROP CONSTRAINT panel_notice_state_check")
    op.execute(
        """
        ALTER TABLE app.panel_notice ADD CONSTRAINT panel_notice_state_check CHECK (
            state IN ('queued', 'delivered', 'shown', 'answered', 'dismissed', 'expired',
                      'withdrawn')
        )
        """
    )
    op.execute("DROP POLICY panel_notice_panel_read ON app.panel_notice")
    op.execute(_read_policy(withdrawn_arm=True))
    # The history the composer lists: the owner's own notices, newest first.
    op.execute("CREATE INDEX panel_notice_sent ON app.panel_notice (sender, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX app.panel_notice_sent")
    op.execute("DROP POLICY panel_notice_panel_read ON app.panel_notice")
    op.execute(_read_policy(withdrawn_arm=False))
    op.execute("DELETE FROM app.panel_notice WHERE state = 'withdrawn'")
    op.execute("ALTER TABLE app.panel_notice DROP CONSTRAINT panel_notice_state_check")
    op.execute(
        """
        ALTER TABLE app.panel_notice ADD CONSTRAINT panel_notice_state_check CHECK (
            state IN ('queued', 'delivered', 'shown', 'answered', 'dismissed', 'expired')
        )
        """
    )
