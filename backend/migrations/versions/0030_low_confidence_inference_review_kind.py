"""Allow the low_confidence_inference review-item kind.

The note→graph Integrator surfaces a fact it held for review (an inferred or
low-weight proposal the arbiter would not auto-commit, or a cross-subject /
ambiguous link) as a `low_confidence_inference` card rather than dropping it
(docs/archive/INTEGRATOR_PLAN.md N11, A1b-ii-2). The review_items kind CHECK is an
explicit allowlist (migration 0023), so the new kind needs admitting here.

Rides the existing app.review_items RLS policy (no new table).

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-13
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

# The kinds admitted by migration 0023, in the same order.
_BASE = (
    "'fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
    " 'domain_promotion', 'low_confidence', 'split_proposal', 'inverse_proposal',"
    " 'extraction_truncated'"
)
_KINDS_WITH = f"({_BASE}, 'low_confidence_inference')"
_KINDS_WITHOUT = f"({_BASE})"


def upgrade() -> None:
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITH})"
    )


def downgrade() -> None:
    # Clear any rows of the new kind before narrowing, or the re-add would fail.
    op.execute("DELETE FROM app.review_items WHERE kind = 'low_confidence_inference'")
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITHOUT})"
    )
