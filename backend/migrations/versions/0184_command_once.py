"""A command that disarms after it fires (APRS_CONTROL_PLAN.md P4).

The third arming mode in the closed mock (`docs/mocks/aprs/b-trigger-editor.html`,
shape A: `Always | Once | Window`, "**Once** disarms after it fires"), which the first
build skipped. It is the delivery-driver command: hand out one code, it works once, and
then the command is off until the owner arms it again.

It is a column rather than a convention because the disarm has to happen in the SAME
statement that burns the counter. Turning the task off afterwards, as a second write,
leaves a window in which a duplicate of that transmission — normal on a digipeated
channel — finds a command that is still armed.
"""

from alembic import op

revision = "0184"
down_revision = "0183"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.tasks ADD COLUMN command_once boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE app.tasks DROP COLUMN command_once")
