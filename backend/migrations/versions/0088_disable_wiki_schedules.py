"""Disable the nightly wiki schedules again (operator hold).

Migration 0048 enabled the nightly `wiki_refresh` (03:30) and `wiki_prune` (03:45) once the
live LLM rewriter was wired. The two wiki LLM tasks (`wiki.rewrite`, `wiki.ground`) were never
registered in the router's TASK_DEFAULTS, so every build aborted with `unknown LLM task`. The
tasks are now registered, but the owner wants the wiki to stay OFF the automatic nightly
schedule for now — to be exercised manually (Ops `wiki_rebuild`/`wiki_refresh`) before it
runs unattended. So set both schedules disabled again; the actions remain Ops-fireable.

Revision ID: 0088
Revises: 0087
Create Date: 2026-06-23
"""

from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None

_REFRESH = "00000000-0000-0000-0000-0000000f0001"
_PRUNE = "00000000-0000-0000-0000-0000000f0003"


def upgrade() -> None:
    op.execute(f"UPDATE app.schedules SET enabled = false WHERE id IN ('{_REFRESH}', '{_PRUNE}')")


def downgrade() -> None:
    op.execute(f"UPDATE app.schedules SET enabled = true WHERE id IN ('{_REFRESH}', '{_PRUNE}')")
