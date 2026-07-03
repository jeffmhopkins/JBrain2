"""Allow the shape_mismatch review-item kind (EMR import §6.6).

A malformed lab `value_json` must never be silently degraded to a statement-only
fact the projection can't chart (`value_shape_enforce`'s default nulls the value).
Lab integration instead raises a `shape_mismatch` card and holds the reading
pending_review with its value_json intact for owner correction. The review_items
kind CHECK is an explicit allowlist, so the new kind needs admitting here — the
ONLY genuinely new review kind this feature adds (every other degraded outcome
maps to an existing kind + a subkind payload discriminator).

Rebuilt from the CURRENT twelve-kind list (through 0034's `_BASE` + confirm_entity)
plus 'shape_mismatch' — NOT the 0006 baseline (that would silently drop the five
post-0006 kinds). Lands in Wave 1, before any code references the kind. Rides the
existing app.review_items RLS policy (no new table).

Revision ID: 0118
Revises: 0117
Create Date: 2026-07-03
"""

from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None

# The twelve kinds admitted through migration 0034, in the same order.
_BASE = (
    "'fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
    " 'domain_promotion', 'low_confidence', 'split_proposal', 'inverse_proposal',"
    " 'extraction_truncated', 'low_confidence_inference', 'new_predicate', 'confirm_entity'"
)
_KINDS_WITH = f"({_BASE}, 'shape_mismatch')"
_KINDS_WITHOUT = f"({_BASE})"


def upgrade() -> None:
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITH})"
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.review_items WHERE kind = 'shape_mismatch'")
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITHOUT})"
    )
