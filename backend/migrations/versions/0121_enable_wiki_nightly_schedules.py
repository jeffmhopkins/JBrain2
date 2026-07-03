"""Enable the nightly wiki schedules: builder (refresh + prune) and the health sweep (wiki_lint).

The wiki builder's nightly schedules were seeded disabled (0047), enabled (0048), then held OFF
again by operator choice (0088) to exercise the wiki manually first. `wiki_lint` likewise seeded
disabled (0119). The owner has now opted the wiki into automatic nightly operation, so this flips
all three nightly schedules ON — the next `jbrain update` (which runs migrations) enables them:

- `nightly_wiki_refresh` (03:30) — dirty-bit-driven incremental article build.
- `nightly_wiki_prune`   (03:45) — archive orphaned articles, after the refresh.
- `nightly_wiki_lint`    (04:00) — corpus-wide health audit, after the prune.

`wiki_rebuild` / `wiki_reindex` stay Ops-manual (their schedules remain disabled — a full re-derive
or index re-embed is on-demand, not nightly). The scheduler's `advance` is "next run one interval
from now, never replays missed" (scheduler.advance), so each stale `next_run_at` fires once on the
next tick — kicking off the first build/lint over the existing dirty backlog — then settles to a
daily cadence. Idempotent flip; mirrors 0048/0088.

NOTE (routing): the wiki LLM tasks (`wiki.rewrite`/`wiki.ground` and the Wave-B
`wiki.lint.contradiction`/`wiki.lint.stale`) default to `xai:grok-4.3`; a deploy without a working
xAI key must route them to a local model via the live per-task overrides, or the nightly build/lint
will fail closed on the provider.

Revision ID: 0121
Revises: 0120
Create Date: 2026-07-03
"""

from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

# Nightly wiki schedule ids (0047 for the builder, 0119 for the health sweep).
_REFRESH = "00000000-0000-0000-0000-0000000f0001"
_PRUNE = "00000000-0000-0000-0000-0000000f0003"
_LINT = "00000000-0000-0000-0000-0000000c0021"
_IDS = f"('{_REFRESH}', '{_PRUNE}', '{_LINT}')"


def upgrade() -> None:
    op.execute(f"UPDATE app.schedules SET enabled = true WHERE id IN {_IDS}")


def downgrade() -> None:
    op.execute(f"UPDATE app.schedules SET enabled = false WHERE id IN {_IDS}")
