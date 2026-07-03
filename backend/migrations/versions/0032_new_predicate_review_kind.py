"""Allow the new_predicate review-item kind.

Embedding-assisted predicate canonicalization (docs/reference/PREDICATE_CANONICALIZATION.md
Phase 3) files a `new_predicate` card when an unknown predicate has no confident
canonical match — the fact still commits under its raw name, and the card lets
the owner/agent keep it or map it onto a suggested canonical. The review_items
kind CHECK is an explicit allowlist (migration 0023), so the new kind needs
admitting here.

Rides the existing app.review_items RLS policy (no new table).

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-14
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

# The kinds admitted through migration 0030, in the same order.
_BASE = (
    "'fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
    " 'domain_promotion', 'low_confidence', 'split_proposal', 'inverse_proposal',"
    " 'extraction_truncated', 'low_confidence_inference'"
)
_KINDS_WITH = f"({_BASE}, 'new_predicate')"
_KINDS_WITHOUT = f"({_BASE})"


def upgrade() -> None:
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITH})"
    )


def downgrade() -> None:
    # Clear any rows of the new kind before narrowing, or the re-add would fail.
    op.execute("DELETE FROM app.review_items WHERE kind = 'new_predicate'")
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITHOUT})"
    )
