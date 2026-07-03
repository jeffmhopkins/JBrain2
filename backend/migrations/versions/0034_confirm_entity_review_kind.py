"""Allow the confirm_entity review-item kind.

Provisional -> confirmed entity promotion (docs/reference/entity.md "Entity lifecycle")
auto-confirms an entity once enough distinct notes corroborate it, but when the
identity is contested (a live namesake) it files a `confirm_entity` card instead
of cementing a possibly-wrong identity. The review_items kind CHECK is an
explicit allowlist, so the new kind needs admitting here.

Rides the existing app.review_items RLS policy (no new table).

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-15
"""

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

# The kinds admitted through migration 0032, in the same order.
_BASE = (
    "'fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
    " 'domain_promotion', 'low_confidence', 'split_proposal', 'inverse_proposal',"
    " 'extraction_truncated', 'low_confidence_inference', 'new_predicate'"
)
_KINDS_WITH = f"({_BASE}, 'confirm_entity')"
_KINDS_WITHOUT = f"({_BASE})"


def upgrade() -> None:
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITH})"
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.review_items WHERE kind = 'confirm_entity'")
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITHOUT})"
    )
