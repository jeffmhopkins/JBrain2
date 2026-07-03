"""Allow the wiki_contradiction and wiki_stale_claim review-item kinds (wiki_lint Wave B).

The corpus-wide wiki health sweep (docs/plans/WIKI_LINT_PLAN.md) Wave B files owner-judgment
cards for two LLM-verified drift classes: a cross-article contradiction (two firewall-compatible
subjects' articles disagree) and a stale claim (an article's prose frames a superseded fact as
current). The review_items kind CHECK is an explicit allowlist, so the new kinds need admitting.

Rides the existing app.review_items RLS policy (no new table → no new isolation test).

Revision ID: 0120
Revises: 0119
Create Date: 2026-07-03
"""

from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None

# The current kind list through migration 0118 (0034's twelve + 'shape_mismatch'), in order —
# NOT the 0006 baseline, which would silently drop the post-0006 kinds when the CHECK is rebuilt.
_BASE = (
    "'fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',"
    " 'domain_promotion', 'low_confidence', 'split_proposal', 'inverse_proposal',"
    " 'extraction_truncated', 'low_confidence_inference', 'new_predicate', 'confirm_entity',"
    " 'shape_mismatch'"
)
_KINDS_WITH = f"({_BASE}, 'wiki_contradiction', 'wiki_stale_claim')"
_KINDS_WITHOUT = f"({_BASE})"


def upgrade() -> None:
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITH})"
    )


def downgrade() -> None:
    # Clear any rows of the new kinds before narrowing, or the re-add would fail.
    op.execute(
        "DELETE FROM app.review_items WHERE kind IN ('wiki_contradiction', 'wiki_stale_claim')"
    )
    op.execute("ALTER TABLE app.review_items DROP CONSTRAINT review_items_kind_check")
    op.execute(
        "ALTER TABLE app.review_items ADD CONSTRAINT review_items_kind_check"
        f" CHECK (kind IN {_KINDS_WITHOUT})"
    )
