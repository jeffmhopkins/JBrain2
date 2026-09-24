"""`endpoint_panel` — the settings that are one panel's rather than the box's.

`app.endpoint_settings` is ONE ROW, `id = 1`, and that was right for what it holds: volume,
microphone gain, brightness and the debug overlay are the same answer for every unit in the
house. These three are not.

- **`pet_name` is the wake word.** `vocab.c` compiles in `{"hey fish", VOCAB_LISTEN, 0}`, and
  `vocab_name()` takes the last word of that phrase for the label drawn above the pet's head.
  So the creature is called *fish* because a C array says so, and the file has said since it was
  written that this is a gap: *"the owner has no terminal, so a name only a rebuild can change is
  a name they cannot change, and the two panels will want different ones. It belongs on
  `endpoint_settings` beside the other knobs."* It belongs here instead, because the same
  sentence explains why: the two panels will want DIFFERENT ones, and `endpoint_settings` can
  only hold one answer.
- **`form` is which body is drawn.** `display.c` toggles `FORM_OSTRICH`/`FORM_ROBOT` on four
  taps and a hold, in RAM, and nothing writes it down — so every reboot and every OTA puts both
  twins back to an ostrich. A child who chose the robot loses it to an update she did not ask
  for, which reads as the panel undoing her.

**The owner writes; a panel only reads.** Deliberately, and it is the difference between a
DEFAULT and a setting: the gesture stays a live toggle that costs nothing to try, and the body
the panel comes back as is the one the owner chose. A panel that could write this row would
turn every accidental four-tap into a permanent change nobody made on purpose.

**Keyed on the SUBJECT, which only became a stable thing to key on in 0211's wake.** A flash used
to mint a fresh subject every time, so a row keyed here would have been orphaned by the next
re-flash — losing a pet's name and a chosen body at exactly the moment the owner is most likely
to be re-flashing. The flash now rotates the existing subject when the name and role match, so
"this panel" is a row that outlives the credentials hanging off it.

Revision ID: 0212
Revises: 0211
Create Date: 2026-09-24
"""

from alembic import op

revision = "0212"
down_revision = "0211"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.endpoint_panel (
            subject_id uuid PRIMARY KEY REFERENCES app.subjects(id) ON DELETE CASCADE,
            -- EMPTY MEANS "whatever the firmware was built with", not "no name". A panel that
            -- has never been given one must keep answering to the wake word it shipped with:
            -- a blank name would leave a child saying something the panel cannot hear.
            pet_name text NOT NULL DEFAULT '',
            form text NOT NULL DEFAULT 'ostrich' CHECK (form IN ('ostrich', 'robot')),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute("ALTER TABLE app.endpoint_panel ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.endpoint_panel FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY endpoint_panel_owner ON app.endpoint_panel
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # FOR SELECT, and only its own row. A panel needs to know what it is called and what body to
    # wear; it has no business reading its sibling's settings, and no business writing either —
    # `WITH CHECK` is absent because there is no write to check. `0178_settings_deny_jmolt` is
    # the argument for why "the handler only reads" is a code-review convention and this is not.
    op.execute(
        """
        CREATE POLICY endpoint_panel_own ON app.endpoint_panel FOR SELECT
        USING (
            current_setting('app.principal_kind', true) = 'device_key'
            AND subject_id::text = current_setting('app.subject_id', true)
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.endpoint_panel TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.endpoint_panel")
