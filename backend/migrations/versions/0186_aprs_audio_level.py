"""How strong each transmission was.

The AX.25 frame does not carry it — it is a property of the radio link, known only to
whatever demodulated the audio. Direwolf reports it on a log line per decode, and an
earlier wave concluded it was unrecoverable because we ship direwolf with `-q hd`: `h`
means precisely "suppress the heard line with the audio level". A flag, not a limit.

**Nullable, and NULL means "not measured".** It is never "weak" and never zero. Rows
written before this migration have no level and never will — the reading existed only
at decode time, so unlike every column in 0185 this one CANNOT be backfilled from
`raw`. That asymmetry is the whole reason it is a column of its own rather than another
derived field: the sweep must not treat a missing level as work to do.

Nullable-added columns are also what lets the previous release's code keep serving
during a rolling restart (docs/reference/DEVELOPMENT.md).
"""

from __future__ import annotations

from alembic import op

revision = "0186"
down_revision = "0185"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.aprs_packets
            -- Direwolf's own 0-100 reading for the decode that produced this row.
            -- smallint: the range is fixed and one byte of headroom is plenty.
            ADD COLUMN audio_level smallint
        """
    )
    # A reading outside direwolf's own range is a bug in the pairing, not data. The
    # sidecar already clamps; this makes the guarantee the table's rather than the
    # sender's, which is the half that survives a change of sidecar.
    op.execute(
        """
        ALTER TABLE app.aprs_packets
            ADD CONSTRAINT aprs_packets_audio_level_range
            CHECK (audio_level IS NULL OR audio_level BETWEEN 0 AND 100)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.aprs_packets DROP CONSTRAINT aprs_packets_audio_level_range")
    op.execute("ALTER TABLE app.aprs_packets DROP COLUMN audio_level")
