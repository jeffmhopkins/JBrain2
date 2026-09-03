"""What each heard frame actually is, as columns you can filter on.

The AX.25 header does not say what a packet channel contains. Measured on the owner's box
over 90 minutes on 144.390: 184 frames, **5 values in `source`, 15 real senders** — three
quarters of the log was one IGate relaying internet traffic as third-party frames, and for
every one of those `source` names the RELAY rather than the sender.

So `aprs_packets_source_idx`, whose own comment calls it "what has this callsign sent",
answers a different question than it claims for most of the table. These columns are the
correction, and they are what make "filter by station" and "filter by type" possible at
all (docs/mocks/aprs/e-stations.html, the binding spec).

**Every column here is a CACHE over `raw`**, which is stored losslessly and never
truncated. A better classifier can be backfilled at any time, so getting the derivation
wrong today costs a re-run and never a row. That is also why they are nullable: rows
written before this migration are filled in by a self-healing sweep rather than a blocking
one-shot, and a row the classifier cannot read stays NULL rather than lying.

Nullable-added columns are also what lets the previous release's code keep serving during
a rolling restart (docs/reference/DEVELOPMENT.md).
"""

from alembic import op

revision = "0185"
down_revision = "0184"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.aprs_packets
            -- The TRUE sender: the station inside a third-party wrapper, else `source`.
            ADD COLUMN origin_call text,
            -- The effective APRS data-type identifier, taken from `raw` so the two Mic-E
            -- identifiers that are control bytes survive the info-field scrub.
            ADD COLUMN data_type text,
            -- One of Position | Message | Weather | Object | Other. Five buckets rather
            -- than APRS's ~25 identifiers: a phone filter with fifteen equal chips is
            -- worse than one with five that matter.
            ADD COLUMN kind text,
            -- Came from the internet: third-party AND carrying TCPIP/TCPXX. NOT the same
            -- as third-party — an RF relay or a satellite downlink is wrapped too, and
            -- those frames genuinely were heard on the air.
            ADD COLUMN gated boolean,
            -- Nobody repeated it: no digipeater marked itself in the path, so this is the
            -- sender's own transmission as we received it.
            ADD COLUMN heard_direct boolean,
            -- For a message, who it is addressed to. This is how the owner's own mail is
            -- found — and it arrives wrapped, because an IGate relays a message to RF
            -- only when the addressee has been heard nearby.
            ADD COLUMN addressee text
        """
    )
    # The corrected version of the source index. "Which stations, most recent first" is
    # the roster's only query, and "this station's traffic" is the detail view's.
    op.execute(
        "CREATE INDEX aprs_packets_origin_idx ON app.aprs_packets (origin_call, heard_at DESC)"
    )
    # The backfill sweep's own predicate: it claims rows that have not been classified.
    # Partial, so it costs nothing once the table is fully derived — the index is empty.
    op.execute(
        "CREATE INDEX aprs_packets_unclassified_idx ON app.aprs_packets (heard_at DESC)"
        " WHERE kind IS NULL"
    )
    # `aprs_packets_source_idx` is deliberately LEFT IN PLACE. It still answers "what did
    # this relay put on the air", which is a real question, and dropping an index in the
    # same migration that adds its replacement means a rollback has neither.


def downgrade() -> None:
    op.execute("DROP INDEX app.aprs_packets_unclassified_idx")
    op.execute("DROP INDEX app.aprs_packets_origin_idx")
    op.execute(
        """
        ALTER TABLE app.aprs_packets
            DROP COLUMN origin_call,
            DROP COLUMN data_type,
            DROP COLUMN kind,
            DROP COLUMN gated,
            DROP COLUMN heard_direct,
            DROP COLUMN addressee
        """
    )
