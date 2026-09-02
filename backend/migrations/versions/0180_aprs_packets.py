"""The APRS heard log: what the radio decoded, stored as rows.

docs/plans/APRS_CONTROL_PLAN.md P1. A second job for the SDR — direwolf decodes AX.25
frames off a packet frequency and each one becomes a row here.

**Owner-only, and that is the whole policy.** The radio is a physical device on the
owner's box; there is no scoped-token or family case for what it overheard. So the
table gets `app.is_owner()` and nothing else, and a non-owner session sees an empty
table rather than a filtered one (CLAUDE.md #3 — the firewall is Postgres, not the
caller).

**`info` is UNTRUSTED TEXT.** These bytes came off the air from anyone with a
transmitter, and a source callsign is plain bytes that forge trivially. Nothing
downstream may treat a row here as instructions — a packet reaching a model as a
prompt is prompt injection with an antenna (the plan's two trust tiers). The column is
storage, not a channel.

**`raw` is kept** so a parser bug found later can be re-run against what was actually
heard, rather than the traffic being gone. It is hex rather than bytea because it is
read far more often than it is written, and reading it in psql or the debug console
should not need decoding.

Retention is deliberately NOT enforced here: a busy channel is a lot of rows, and
what to keep is an owner decision the plan holds open (§7). An index on `heard_at`
makes both the log view and a future prune cheap.
"""

from alembic import op

revision = "0180"
down_revision = "0179"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.aprs_packets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            heard_at timestamptz NOT NULL DEFAULT now(),
            frequency_hz bigint NOT NULL,
            source text NOT NULL,
            destination text NOT NULL,
            path text[] NOT NULL DEFAULT '{}',
            info text NOT NULL,
            raw text NOT NULL
        )
        """
    )
    # Newest-first is the only order the log is ever read in.
    op.execute("CREATE INDEX aprs_packets_heard_idx ON app.aprs_packets (heard_at DESC)")
    # "What has this callsign sent" is the second question, and the one a command
    # path asks on every verified frame.
    op.execute("CREATE INDEX aprs_packets_source_idx ON app.aprs_packets (source, heard_at DESC)")

    op.execute("ALTER TABLE app.aprs_packets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.aprs_packets FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY aprs_packets_owner ON app.aprs_packets
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # RLS decides WHICH rows; the grant decides whether the app role may reach the table
    # at all. Without this every query is `permission denied` before a policy is ever
    # consulted — the log would be write-only in the sense that it never writes.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.aprs_packets TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.aprs_packets")
