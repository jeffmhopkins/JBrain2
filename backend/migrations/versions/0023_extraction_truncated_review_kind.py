"""Allow the extraction_truncated review-item kind.

When a note holds more durable facts than its per-note budget, the analysis
pipeline files an informational `extraction_truncated` card so the clipped
tail is visible rather than silently dropped (docs/reference/ANALYSIS.md "Over-extraction
is the known quality risk"). The review_items kind CHECK is an explicit
allowlist, so the new kind needs admitting here.

Rides the existing app.review_items RLS policy (no new table).

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-13
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_KINDS_WITH = (
    "('fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
    " 'domain_promotion', 'low_confidence', 'split_proposal', 'inverse_proposal',"
    " 'extraction_truncated')"
)
_KINDS_WITHOUT = (
    "('fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
    " 'domain_promotion', 'low_confidence', 'split_proposal', 'inverse_proposal')"
)


def upgrade() -> None:
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITH})"
    )


def downgrade() -> None:
    # Clear any rows of the new kind before narrowing the constraint, or the
    # re-add would fail on existing data.
    op.execute("DELETE FROM app.review_items WHERE kind = 'extraction_truncated'")
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITHOUT})"
    )
