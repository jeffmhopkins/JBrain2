"""`subjects.device_role` — what kind of endpoint a device is, as a column rather than a prefix.

Panels and phones are the same substrate: `/endpoint/flash` calls the same `provision_device`
the phone pairing flow does, so a panel is a `Subject(kind='device')` with a `device_key`, exactly
like an OwnTracks phone. Nothing recorded which one had been provisioned, so three separate
queries reconstructed it from the label — `p.label LIKE 'panel%' OR p.label = 'room endpoint panel'`
— and `_panel_names` said so in as many words: *"THIS IS A LABEL CONVENTION, NOT A MECHANISM."*

It held until it didn't. The owner flashed a third unit, took the unnamed default, and that
label matched the roster predicate: the panel opted itself into the twins' addressing by being
named nothing in particular. `send(to="panel")` needs exactly two and there were three, so voice
messages between the twins stopped and the sibling name went blank. Meanwhile the Location
screen's phone list — `WHERE s.kind = 'device'`, no further filter — had been listing every panel
ever flashed, one row per flash, each rendering a status line (`last seen`, battery, fix count)
that a panel structurally never produces. Fifteen rows of "no fixes yet" is not a device list.

Both are the same missing fact, so it becomes a column:

- `NULL` — a phone. It writes location fixes and belongs on the Location screen.
- `'jpet'` — one of the twins' pet panels. In the roster, addressable by its sibling.
- `'display'` — an endpoint the owner operates: OTA, settings, telemetry, `/converse`. Visible in
  the fleet view like any other, and NOT in the twin roster.

**The backfill is deliberately the old predicate, verbatim.** Every device whose label the roster
would have matched becomes `'jpet'`; everything else stays `NULL`. An upgrade therefore moves
nobody: the fleet the owner sees after this migration is the fleet he saw before it, and the
three-panel breakage is his to fix by revoking, not something a migration should silently repair
by guessing which unit was the odd one out.

**A device cannot set its own role.** `subjects_access` already reads
`USING (app.is_owner() OR id = app.subject_id) WITH CHECK (app.is_owner())`, so a panel may read
its own subject row — which is what lets `/settings` hand it its role — and may not write one.
That asymmetry is the point rather than an accident of an old policy: the role decides whether a
device is inside the twins' addressing, and a display box on the owner's desk must not be able to
promote itself into two children's bedrooms by claiming to be a pet.

Revision ID: 0211
Revises: 0210
Create Date: 2026-09-23
"""

from alembic import op

revision = "0211"
down_revision = "0210"
branch_labels = None
depends_on = None

# The predicate the roster used before this migration existed, kept in one place so the backfill
# and the test that pins it cannot drift apart.
LEGACY_PANEL_LABEL = "(p.label LIKE 'panel%' OR p.label = 'room endpoint panel')"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.subjects ADD COLUMN device_role text
            CHECK (device_role IS NULL OR device_role IN ('jpet', 'display'))
        """
    )
    # A phone is the overwhelming majority and reads as NULL, so the index only carries endpoints.
    op.execute(
        "CREATE INDEX subjects_device_role_idx ON app.subjects (device_role)"
        " WHERE device_role IS NOT NULL"
    )
    op.execute(
        f"""
        UPDATE app.subjects s SET device_role = 'jpet'
        WHERE s.kind = 'device'
          AND EXISTS (
              SELECT 1 FROM app.principals p
              WHERE p.subject_id = s.id AND p.kind = 'device_key' AND {LEGACY_PANEL_LABEL}
          )
        """
    )

    # `_panel_names` — the roster that answers "who is this panel's sibling" — runs under the
    # narrow `login` context ON PURPOSE: reading it under a panel's own context returned exactly
    # one row (itself), which is why panel-to-panel messaging answered 409 from the first commit.
    # `principals_select` already trusts `login`, with the whole `key_hash` column in it. Now
    # that the roster must join `subjects` to read the role, `login` needs to see that table
    # too, and a SELECT-only policy of its own is how: widening `subjects_access` would have
    # extended `login`'s reach to UPDATE and DELETE as well, since one `FOR ALL` policy's
    # `USING` governs all three. Policies OR together, so this adds read and nothing else.
    op.execute(
        """
        CREATE POLICY subjects_select_login ON app.subjects FOR SELECT
        USING (app.auth_ctx() = 'login')
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS subjects_select_login ON app.subjects")
    op.execute("DROP INDEX IF EXISTS app.subjects_device_role_idx")
    op.execute("ALTER TABLE app.subjects DROP COLUMN device_role")
