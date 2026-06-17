"""The predicate_aliases store (Loop 3a, Wave 1; docs/LOOP3_PREDICATE_CANON_PLAN.md).

A durable raw->canonical predicate alias. Today a resolved `new_predicate` card
(`map_to_existing` / renaming `suggest_better`) only heals the *stored* facts — there is no
runtime alias store the canonicalize path consults, so the next run re-emits the drift spelling
and re-files the card (`analysis/repo.py` TODO). This table closes that loop: `decide_predicates`
consults it first, so a confirmed drift spelling collapses to its canonical at canonicalize time
with no embed and no new card.

Keyed by the registry's `_norm_key(raw)` (case/separator-insensitive), so spelling variants of an
aliased predicate collapse too. Global reference data like `canonical_predicates` (every principal
reads), but self-extending, so writes are gated to the owner/system context (a real RLS policy +
an isolation test, CLAUDE.md rule 3). A `seed` here is never the model's whim — only an
owner-approved resolution writes a row.

Revision ID: 0056
Revises: 0055
Create Date: 2026-06-17
"""

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.predicate_aliases (
            -- _norm_key(raw): the case/separator-insensitive drift spelling
            raw_norm text PRIMARY KEY,
            canonical_name text NOT NULL
                REFERENCES app.canonical_predicates(canonical_name) ON DELETE CASCADE,
            origin text NOT NULL DEFAULT 'review' CHECK (origin IN ('review')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX predicate_aliases_canonical_idx"
        " ON app.predicate_aliases (canonical_name)"
    )

    op.execute("ALTER TABLE app.predicate_aliases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.predicate_aliases FORCE ROW LEVEL SECURITY")
    # Global reference data: every principal reads (canonical_predicates / app.domains precedent).
    op.execute(
        "CREATE POLICY predicate_aliases_read ON app.predicate_aliases FOR SELECT USING (true)"
    )
    # Self-extending: only the owner/system context records an alias (an approved resolution).
    op.execute(
        "CREATE POLICY predicate_aliases_insert ON app.predicate_aliases"
        " FOR INSERT WITH CHECK (app.is_owner())"
    )
    op.execute("GRANT SELECT, INSERT ON app.predicate_aliases TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.predicate_aliases")
