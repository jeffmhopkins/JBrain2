"""`jpanel_message.deliveries` — a panel that cannot acknowledge must not loop forever.

**THIS EXISTS BECAUSE IT HAPPENED.** A firmware bug meant `POST /played` never fired after a
message was played (ROOM_ENDPOINT_PLAN.md §10.4cw): the row stayed unplayed, every poll fetched
it again, and a panel repeated the same message in a child's bedroom every thirty seconds until
new firmware could be built and shipped. Nothing on the box could stop it — the debug SQL
surface is read-only, and `DELETE /messages` deliberately preserves exactly this row.

The firmware bug is fixed. **The class of bug is not**, and it never will be: every future path
to "the panel did not acknowledge" — a crash mid-playback, a flaky link that drops the POST, an
image that regresses this again — ends with the same audio repeating in a bedroom. That is a
property of the BOX, and the box should own it, because the box is the half that can be fixed
without an OTA.

So `GET /next` counts how many times it has handed a message over and stops offering one that
has been collected `JPANEL_MAX_DELIVERIES` times without ever being acknowledged.

**IT DOES NOT MARK THE MESSAGE PLAYED**, and that distinction is the whole design. `played_at`
means a child heard it; writing it here would be the box telling the owner a lie about his
children — and the owner's thread would show the message delivered when nobody ever heard a
word. The row stays unplayed, because it IS unplayed. What changes is only that the panel stops
being offered it, and `deliveries` is on the wire so the PWA can say the honest thing: this one
could not be delivered.

**A counter rather than a timestamp**, because the question is "how many times did we try",
which survives a clock that moved and a panel that rebooted. Incremented under the PANEL's own
context in `GET /next`: `jpanel_message_panel_played` already bounds a panel's UPDATE to rows
addressed to it, so this needs no new policy and a panel still cannot touch its sibling's
counters. RLS bounds rows, not columns — the residual is the one 0208 already states and
accepts.

Revision ID: 0209
Revises: 0208
Create Date: 2026-09-23
"""

from alembic import op

revision = "0209"
down_revision = "0208"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.jpanel_message
            ADD COLUMN deliveries integer NOT NULL DEFAULT 0
        """
    )
    # The inbox query is `recipient_device = ? AND played_at IS NULL` and now also filters on
    # this, so it rides the same index rather than forcing a scan per poll — two panels asking
    # every thirty seconds forever is the access pattern this table was designed around.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS jpanel_message_inbox_idx
            ON app.jpanel_message (recipient_device, created_at)
            WHERE played_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.jpanel_message_inbox_idx")
    op.execute("ALTER TABLE app.jpanel_message DROP COLUMN IF EXISTS deliveries")
