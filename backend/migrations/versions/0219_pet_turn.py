"""`pet_turn` — what the children actually said to the pet, kept for the person raising them.

The owner: *"I think we need some way of logging [what] the kids say to the large language
model ... another tab in the jpanel side that is llm conversations that are stored that I can
clear and read through sorted by panel."*

**IT WAS ALREADY IN THE LOG AND THAT IS NOT THE SAME THING.** Every turn writes an
`endpoint.converse` line carrying `heard` and `reply`, so the words existed — interleaved with
every other event on the box, truncated to 120 characters, rotated away on a schedule nobody
chose for this, and reachable only by reading an access log. A parent asking "what has she been
telling it?" is not going to grep. The words a four-year-old says to a toy that answers back are
the single most interesting thing this box produces, and they were a debug field.

**THE PANEL MAY WRITE THESE AND MAY NEVER READ THEM**, which is the whole of the policy below
and the one decision worth arguing. A panel hangs on a bedroom wall and authenticates with a
device key a four-year-old could hand to a visitor (`0208_jpanel_message`). For messages there is
at least a delivery reason to let a panel read a row addressed to it; here there is none. Nothing
on the panel ever needs to recall a conversation — the pet's own short memory is four minutes of
process RAM in the API (`_panel_memory`), deliberately not this table — so SELECT is the owner's
alone, and a panel cannot read back its own transcripts, let alone its sibling's. The insert
policy pins `device_id` to the panel's own principal for `0208`'s reason: without it one panel
could file words under its sister's name, and on this table that is not a forged message but a
forged account of what a child said.

**NO BLOB, NO AUDIO.** The recording is transient by design — `/endpoint/converse` holds it long
enough to transcribe and lets it go. Storing text is a diary; storing every clip is a wire in a
child's bedroom, and the difference is worth keeping on purpose rather than by omission.

**KEPT UNTIL THE OWNER CLEARS IT.** No expiry column and no sweep, because he asked to be the one
who clears it and a transcript that vanishes on a timer is not a record a parent can rely on. The
trade — unbounded growth — is small and bounded in practice: a turn is a few hundred bytes and
these are two children in one house, so a year of heavy use is measured in megabytes. If that ever
stops being true, the answer is a retention setting he sets, not a default nobody chose.

Revision ID: 0219
Revises: 0218
Create Date: 2026-09-29
"""

from alembic import op

revision = "0219"
down_revision = "0218"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.pet_turn (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- The panel that heard it. Text rather than a foreign key for `jpanel_message`'s
            -- reason: a re-flashed panel gets a new principal and the old rows must survive it,
            -- as the record of a conversation that did happen.
            device_id text NOT NULL,
            heard text NOT NULL,
            reply text NOT NULL,
            -- The timings the turn already computes. Free to store and they answer "why did she
            -- give up waiting" months later, which no amount of re-reading the words can.
            stt_ms integer NOT NULL DEFAULT 0,
            llm_ms integer NOT NULL DEFAULT 0,
            tts_ms integer NOT NULL DEFAULT 0,
            total_ms integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # The owner's read is the only read, and it is always one panel's conversation newest-first.
    op.execute(
        """
        CREATE INDEX pet_turn_by_panel ON app.pet_turn (device_id, created_at DESC)
        """
    )

    op.execute("ALTER TABLE app.pet_turn ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.pet_turn FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY pet_turn_owner ON app.pet_turn
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # INSERT ONLY, AND ONLY UNDER ITS OWN NAME. There is deliberately no panel SELECT policy:
    # see the module docstring. A panel writes what it heard and can never read it back.
    op.execute(
        """
        CREATE POLICY pet_turn_panel_write ON app.pet_turn
        FOR INSERT
        WITH CHECK (
            current_setting('app.principal_kind', true) = 'device_key'
            AND device_id = current_setting('app.principal_id', true)
        )
        """
    )

    # THE GRANT IS SEPARATE FROM THE POLICIES AND BOTH ARE REQUIRED. Postgres checks table
    # privileges FIRST and row policies second, so a table with perfect policies and no grant
    # refuses everyone — which is how this migration first failed its own isolation test, with
    # `permission denied for table pet_turn` on the owner as well as the panels.
    #
    # Granted whole and narrowed by the policies above, exactly as `0208_jpanel_message` does:
    # both the owner and the panels arrive as `jbrain_app`, so the grant cannot be the thing
    # that tells them apart and trying to make it so would only bound the owner too.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.pet_turn TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.pet_turn")
