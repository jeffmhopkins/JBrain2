"""Enable the nightly wiki schedules (Phase-6 Wave C2b).

Wave C2a seeded the wiki schedules DISABLED because the builder shipped with the deterministic
`StubRewriter` (terse placeholder prose). C2b wires the live LLM rewriter behind the grounding
gate + the wiki-build budget, so the nightly `wiki_refresh` (03:30) and `wiki_prune` (03:45) are
now safe to run automatically. `wiki_rebuild`/`wiki_reindex` stay Ops-manual (disabled schedule).

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-17
"""

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

_REFRESH = "00000000-0000-0000-0000-0000000f0001"
_PRUNE = "00000000-0000-0000-0000-0000000f0003"


def upgrade() -> None:
    op.execute(f"UPDATE app.schedules SET enabled = true WHERE id IN ('{_REFRESH}', '{_PRUNE}')")


def downgrade() -> None:
    op.execute(f"UPDATE app.schedules SET enabled = false WHERE id IN ('{_REFRESH}', '{_PRUNE}')")
