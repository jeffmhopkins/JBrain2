"""Backfill value_json onto existing low_confidence_inference review cards.

The review detail's proposed-fact panel renders `predicate → value` from the
card payload's `value_json`; cards filed before that field was added (migration
0032 is this revision's parent, and the pipeline only started writing the field
alongside this change) carry none, so the panel fell back to the whole
`statement` sentence ("People call me Jeff." instead of "Jeff"). Re-running
analysis does not fix them — the inference filer skips an existing open card for
the same note/entity/predicate as a duplicate — so copy each card's value from
the held fact it already links by `fact_id`.

Display-only data on an existing table (no schema change); rides the existing
app.review_items RLS policy.

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-15
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

# Copy value_json from the linked held fact into the card payload, but only
# where the card lacks it and its fact_id resolves to a fact. The fact_id guard
# leaves a card with a null/absent fact_id (unresolved entity at filing) alone —
# it has no fact to read a value from, and its panel keeps the statement floor.
# jsonb_exists (not the `?` operator) so the SQL is driver-agnostic.
BACKFILL_SQL = """
    UPDATE app.review_items AS ri
    SET payload = ri.payload || jsonb_build_object('value_json', f.value_json)
    FROM app.facts AS f
    WHERE ri.kind = 'low_confidence_inference'
      AND NOT jsonb_exists(ri.payload, 'value_json')
      AND ri.payload->>'fact_id' IS NOT NULL
      AND (ri.payload->>'fact_id')::uuid = f.id
"""


def upgrade() -> None:
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    # Display-only: drop the key again. A card written fresh after this revision
    # carries value_json too, so a downgrade also strips those — correct, since
    # the frontend at the parent revision doesn't read the field.
    op.execute(
        "UPDATE app.review_items SET payload = payload - 'value_json'"
        " WHERE kind = 'low_confidence_inference' AND jsonb_exists(payload, 'value_json')"
    )
