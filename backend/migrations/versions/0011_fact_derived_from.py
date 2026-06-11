"""Mark a fact as the pipeline-materialized inverse of another fact.

A directed relationship edge (Jeff.spouse -> Celine) needs its reciprocal
(Celine.spouse -> Jeff) to exist on the object's stream, kept consistent by
construction (docs/research/fix-options/2-mutual-inverse-edges.md, Option 2).
derived_from_fact_id is NULL for a primary, note-sourced fact and points at
the source fact for a derived inverse. The derived row copies the source's
note_id so purge's `DELETE WHERE note_id` deletes it for free; ON DELETE
CASCADE is the backstop that keeps a derived edge from ever outliving its
source. The index serves propagation/purge lookups by source fact.

A new review-item kind, inverse_proposal, lets a cross-subject inverse be
PROPOSED rather than written — attributing a fact to the object's stream
across a subject boundary would be a firewall leak, so the gate files a
proposal instead.

Rides the existing app.facts / app.review_items RLS policies (no new table);
the analysis RLS isolation test adds cases proving a derived row obeys
has_domain_scope and that a cross-subject inverse is proposed, not written.

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-11
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.facts ADD COLUMN derived_from_fact_id uuid"
        " REFERENCES app.facts(id) ON DELETE CASCADE"
    )
    op.execute(
        "CREATE INDEX facts_derived_from_idx ON app.facts (derived_from_fact_id)"
    )
    op.execute(
        "ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check"
    )
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        " CHECK (kind IN"
        " ('fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
        " 'domain_promotion', 'low_confidence', 'split_proposal', 'inverse_proposal'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check"
    )
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        " CHECK (kind IN"
        " ('fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
        " 'domain_promotion', 'low_confidence', 'split_proposal'))"
    )
    op.execute("DROP INDEX app.facts_derived_from_idx")
    op.execute("ALTER TABLE app.facts DROP COLUMN derived_from_fact_id")
