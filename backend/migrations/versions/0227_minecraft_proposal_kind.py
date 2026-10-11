"""Admit the `minecraft` Proposal kind: Minecraft_Dave's staged server changes.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §P1. Dave reaches every server capability, but only
reads run on his word: any other console command and every server action (start, stop,
backups, worlds, rules, settings, the allowlist, the update) stages a Proposal the owner
approves (agent/minecraftadmin.py). A staged Proposal needs its kind in this CHECK.

No new table, so no new RLS surface: `app.proposals` keeps its owner policy.
"""

from __future__ import annotations

from alembic import op

revision = "0227"
down_revision = "0226"
branch_labels = None
depends_on = None

# 0195's _KIND_NEW, verbatim — the constraint as it stands on the live box.
_KIND_OLD = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'predicate-canon', 'egress',"
    " 'intake-link', 'intake-submission', 'owner-prefs',"
    " 'merge', 'remove-library-video', 'remove-research-report')"
)
_KIND_NEW = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'predicate-canon', 'egress',"
    " 'intake-link', 'intake-submission', 'owner-prefs',"
    " 'merge', 'remove-library-video', 'remove-research-report', 'minecraft')"
)


def _set_kind(values: str) -> None:
    op.execute("ALTER TABLE app.proposals DROP CONSTRAINT proposals_kind_check")
    op.execute(
        f"ALTER TABLE app.proposals ADD CONSTRAINT proposals_kind_check CHECK (kind IN {values})"
    )


def upgrade() -> None:
    _set_kind(_KIND_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM app.proposals WHERE kind = 'minecraft'")
    _set_kind(_KIND_OLD)
