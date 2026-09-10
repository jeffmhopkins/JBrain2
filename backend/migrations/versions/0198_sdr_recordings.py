"""`sdr_recordings` — what the radio heard, kept as a file (SDR_RECORDING_PLAN.md R1).

The listen stream already exists; this is the table that lets one of its subscribers be
a recorder. The api opens its own stream to the sidecar and spools it into a blob
(CLAUDE.md #2), and one row here says which blob, when, and on what settings.

**Owner-only, and that is the whole policy** — the same reasoning as
`0180_aprs_packets.py`, which is this migration's template: the radio is a physical
device on the owner's box, so what it recorded has no scoped-token or family case at
all. Hence `app.is_owner()` USING+CHECK, ENABLE **and** FORCE row-level security, the
`GRANT` (RLS decides which rows; the grant decides whether `jbrain_app` may reach the
table at all — without it every query is `permission denied` before a policy is ever
consulted), and **no `domain_code`**: this is not domain-scoped data, it is simply the
owner's.

**`captured_s` beside `duration_s` is the design, not redundancy.** `duration_s` is what
the clip IS now; `captured_s` is what was originally recorded, and a trim changes only
the first. `duration_s < captured_s` is therefore what makes a row "trimmed" — derived
from the two numbers rather than a boolean that can drift out of step with the audio,
and it is also what lets the library total how much disk trimming has given back.

`peaks` is the level envelope the trim sheet draws, computed from the audio at stop and
again after a trim. It is a cache over the blob — an empty array means "ffmpeg could not
read it", never "silence", because a missing waveform must never cost the recording.

`transcript`/`transcribed_at` are R3's and stay NULL until then; the column pair is here
so a transcript does not need a second migration and a second table to arrive.

Revision ID: 0196
Revises: 0195
Create Date: 2026-09-10
"""

from alembic import op

revision = "0198"
down_revision = "0197"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.sdr_recordings (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            started_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz NOT NULL DEFAULT now(),
            duration_s double precision NOT NULL DEFAULT 0,
            captured_s double precision NOT NULL DEFAULT 0,
            frequency_hz bigint NOT NULL,
            mode text NOT NULL,
            bandwidth_hz int,
            gain text,
            serial text,
            blob_sha256 text NOT NULL,
            bytes bigint NOT NULL DEFAULT 0,
            peaks jsonb NOT NULL DEFAULT '[]'::jsonb,
            transcript jsonb,
            transcribed_at timestamptz
        )
        """
    )
    # Newest-first is the only order the library is ever read in.
    op.execute("CREATE INDEX sdr_recordings_started_idx ON app.sdr_recordings (started_at DESC)")

    op.execute("ALTER TABLE app.sdr_recordings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.sdr_recordings FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY sdr_recordings_owner ON app.sdr_recordings
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # RLS decides WHICH rows; the grant decides whether the app role may reach the table
    # at all. Without this every query is `permission denied` before a policy is ever
    # consulted — Record would spool a blob to disk and then fail to write its row.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.sdr_recordings TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.sdr_recordings")
