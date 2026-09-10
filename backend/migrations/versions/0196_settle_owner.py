"""`settle_owners` on `app.facts` and `app.entity_mentions` — who still asserts a row.

The whole-note settle (`analysis/pipeline.settle_note`) retracts every fact of the note
it was not handed and deletes every mention it did not re-assert. Three producers write
one note, so that sweep had been retracting co-writers' rows on the live box
(docs/plans/SETTLE_OWNERSHIP.md S1). This column is the key that scopes it.

**A SET, not one owner.** Two producers reading the same note routinely land the same
identity key — during the D13 window `integrate_note` and `note_converse` both read
every ordinary note, and a salient fact is exactly what both write. A single-owner
column cannot state that: whichever producer it names, the other's claim is invisible,
so either the row is swept while someone still asserts it (silent loss, the bug this
closes) or it is stranded active after everyone stopped (the mirror failure). The set
says the true thing — *the note asserts X as long as any producer still says X* — and
each settle removes only its own claim, retracting when the last one goes.

`text[] NOT NULL DEFAULT ARRAY['analyzer']`. The values are the
`analysis/settle_owner.py` constants, not a DB `CHECK` — a fourth producer should cost
a constant, not a migration.

The default is a DELIBERATE divergence from SETTLE_OWNERSHIP.md S1, which asks for none
so a producer that forgets to stamp fails its own INSERT. The loudness is kept, just
moved: no code in `src/` writes these tables by hand — every production row goes through
`AnalysisPipeline.commit_facts`, where the producer is a required keyword with no
default — and `tests/unit/test_settle_owner.py` fails the day a raw INSERT, an
`insert(Fact)` or a bare `Fact(...)` appears without a stamp (declarative constructors
take `**kw: Any`, so pyright alone would not catch those). What the default actually
reaches is ~50 raw-SQL fixture inserts across 26 integration test files, all of them
standing in for analyzer rows.

Backfill is deterministic from `extractor` (`facts.extractor` is `text NOT NULL` since
0006 and every writer sets it): `note_ingest`/`note_ingest_reply` -> `conversation`,
`emr:deterministic` -> `emr`, everything else (the analyzer's `provider:model`, whatever
model wrote it) -> `analyzer`. Existing rows get a one-element set: nothing in the
corpus records a second producer's claim, and the first settle of each producer
re-states its own.

`entity_mentions` carries no writer at all, so its rows backfill to `{analyzer}`:
nothing distinguishes an existing conversation mention, and those rows are the ones
today's unscoped reconcile deletes anyway.

Revision ID: 0196
Revises: 0195
Create Date: 2026-09-10
"""

from alembic import op

revision = "0196"
down_revision = "0195"
branch_labels = None
depends_on = None

# The one place the extractor -> producer mapping is written as SQL. It mirrors
# `analysis/settle_owner.py`'s constants; the module is not imported here because a
# migration must keep meaning what it meant on the day it ran.
_BACKFILL = """
    UPDATE app.facts SET settle_owners = ARRAY[CASE
        WHEN extractor IN ('note_ingest', 'note_ingest_reply') THEN 'conversation'
        WHEN extractor = 'emr:deterministic' THEN 'emr'
        ELSE 'analyzer'
    END]
"""


def upgrade() -> None:
    # Added nullable, backfilled from `extractor`, THEN made NOT NULL: the default only
    # governs rows written without the column, never the ones already in the corpus.
    op.execute("ALTER TABLE app.facts ADD COLUMN settle_owners text[]")
    op.execute(_BACKFILL)
    op.execute(
        "ALTER TABLE app.facts ALTER COLUMN settle_owners SET NOT NULL,"
        " ALTER COLUMN settle_owners SET DEFAULT ARRAY['analyzer']::text[]"
    )

    op.execute("ALTER TABLE app.entity_mentions ADD COLUMN settle_owners text[]")
    op.execute("UPDATE app.entity_mentions SET settle_owners = ARRAY['analyzer']")
    op.execute(
        "ALTER TABLE app.entity_mentions ALTER COLUMN settle_owners SET NOT NULL,"
        " ALTER COLUMN settle_owners SET DEFAULT ARRAY['analyzer']::text[]"
    )
    # No index: both destructive halves filter `note_id` first, which `facts_note_idx`
    # (0006) and the mention table's note index already serve, and the per-note row
    # count is small. A GIN index on the array would need `btree_gin` to be composite
    # with note_id, which is not worth an extension on this corpus.


def downgrade() -> None:
    # Not the inverse of `upgrade`, and the asymmetry is lossy in the dangerous
    # direction: the backfill above reconstructs ONE owner from `extractor`, so it can
    # restore a single-producer row and never a co-assertion. Every claim set this
    # column records after the day it ships — the rows two producers both assert, which
    # is the ordinary case this whole column exists to state — is destroyed here and
    # cannot be rebuilt on the way back up. Worse than losing it: re-upgrading REWRITES
    # those rows as single-owner from `extractor`, i.e. from whichever producer last
    # touched the row (usually the analyzer, which re-extracts every note), handing the
    # analyzer sole retraction rights over facts the conversation still asserts. That is
    # the silent-loss bug 0196 closes, restored quietly and with no error anywhere.
    # So: down-migrate to ABANDON the column, never as one leg of a round trip.
    op.execute("ALTER TABLE app.entity_mentions DROP COLUMN IF EXISTS settle_owners")
    op.execute("ALTER TABLE app.facts DROP COLUMN IF EXISTS settle_owners")
