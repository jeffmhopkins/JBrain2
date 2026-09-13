"""`sdr_recordings.kind` — a recording is audio OR captions
(docs/plans/SDR_RECORDING_PLAN.md R4).

Long-pressing Record swaps what the button captures: the live MP3 as before, or the
closed captions — a transcript of the same received audio, and **no audio at all**. The
owner chose captions INSTEAD of audio rather than beside it, so a captions row has no
blob, no byte count and no waveform, and this is the column that says which it is.

**Every existing row is `'audio'`**, which is what they have always been — the default
is there so this migration states that fact rather than asking the deploy to fill it in.

**The audio-only columns lose their NOT NULL, and their DEFAULT with it.** That pairing
is the point. `bytes bigint NOT NULL DEFAULT 0` on a row with no audio does not read as
"no measurement", it reads as "zero bytes", and `peaks DEFAULT '[]'` reads as an
envelope that was computed and came back flat. This repo's recurring failure is a value
shaped like a measurement that is in fact fiction (SDR_RECORDING_PLAN.md §7 — a trim
that wrote the length it was ASKED for and deleted the original), so a captions row
carries NULL in all three and the disk meter, the library and the trim sheet can each
ask "is there audio here" and get an answer instead of a zero.

**The CHECK is what makes that a schema fact rather than a convention.** Both shapes are
enumerated in one constraint, so `kind` is also bounded to the two values it may take:
an audio row must have all three, a captions row none of them. Without it, "a captions
row has no blob" would hold only for as long as every writer remembered, and the writer
that forgot would be found by `blob_refs` handing out a NULL digest.

`transcript`/`transcribed_at` are NOT added here: 0198 created them for a transcription
wave that never ran, and this fills them by the cheaper route. That is the whole reason
this migration is one column and three loosened constraints.

Revision ID: 0201
Revises: 0200
Create Date: 2026-09-12
"""

from alembic import op

revision = "0201"
down_revision = "0200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.sdr_recordings ADD COLUMN kind text NOT NULL DEFAULT 'audio'")
    for column in ("blob_sha256", "bytes", "peaks"):
        op.execute(f"ALTER TABLE app.sdr_recordings ALTER COLUMN {column} DROP NOT NULL")
    # The defaults go with the NOT NULLs. A default is what turns "this row has no audio"
    # into "this row has zero bytes and a flat waveform" on the next INSERT that omits
    # them, which is exactly the lie the nullability is here to prevent.
    for column in ("bytes", "peaks"):
        op.execute(f"ALTER TABLE app.sdr_recordings ALTER COLUMN {column} DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE app.sdr_recordings ADD CONSTRAINT sdr_recordings_kind_shape CHECK (
            (kind = 'audio'
             AND blob_sha256 IS NOT NULL AND bytes IS NOT NULL AND peaks IS NOT NULL)
            OR
            (kind = 'captions'
             AND blob_sha256 IS NULL AND bytes IS NULL AND peaks IS NULL)
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.sdr_recordings DROP CONSTRAINT sdr_recordings_kind_shape")
    # Captions rows cannot survive a column that no longer exists to explain them, and
    # they hold no audio to keep: they go, and what is left is what the table meant
    # before this migration.
    op.execute("DELETE FROM app.sdr_recordings WHERE kind <> 'audio'")
    op.execute("ALTER TABLE app.sdr_recordings ALTER COLUMN bytes SET DEFAULT 0")
    op.execute("ALTER TABLE app.sdr_recordings ALTER COLUMN peaks SET DEFAULT '[]'::jsonb")
    for column in ("blob_sha256", "bytes", "peaks"):
        op.execute(f"ALTER TABLE app.sdr_recordings ALTER COLUMN {column} SET NOT NULL")
    op.execute("ALTER TABLE app.sdr_recordings DROP COLUMN kind")
