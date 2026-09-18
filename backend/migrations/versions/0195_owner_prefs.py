"""`owner_prefs` — the owner's standing instructions for note ingestion (D15).

A single owner-only Markdown document, one row per principal, injected into every
note conversation's prompt ahead of the note
(docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, W3). Same shape and same owner-only
firewall as `archivist_memory` (0094): `app.is_owner()` USING+CHECK, ENABLE **and**
FORCE row-level security, one row updated in place — so SELECT/INSERT/UPDATE/DELETE
are all granted.

`content` is a rule per LINE, not free Markdown: the delta ops (`prefs_write`) address
a rule by its number in that list, and a rule that could contain its own newline would
let one edit rewrite the numbering of every rule after it. The application enforces
that; the column stores the joined result.

Also admits the `owner-prefs` Proposal kind. `prefs_write` never writes this table — it
stages a Proposal the owner approves (D17), and a staged Proposal needs its kind in the
closed CHECK 0111 last widened.

Revision ID: 0195
Revises: 0194
Create Date: 2026-09-09
"""

from alembic import op

revision = "0195"
down_revision = "0194"
branch_labels = None
depends_on = None

# 0111's own _KIND_NEW, verbatim — the constraint as it stands on the live box. Restating
# it by hand is how `prompt-edit` and `skill-promotion` got dropped from the first draft of
# this migration, which would have silently un-admitted two kinds nothing here touches.
_KIND_OLD = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'predicate-canon', 'egress',"
    " 'intake-link', 'intake-submission')"
)
# Four additions. `owner-prefs` is this migration's own; the other three are a live bug
# this surfaced — `propose_merge` (mergetools), the library-video removal (externaltools)
# and the research-report removal (researchtools) have all been staging kinds the CHECK
# refuses, so each raises at stage time on a real box. Every test covering them uses a
# fake repo, which is exactly why nine months of green CI never noticed.
# `test_proposal_kinds_pg.py` now stages every kind the source actually constructs, so a
# tool added without its kind fails there rather than on the owner's box.
_KIND_NEW = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'predicate-canon', 'egress',"
    " 'intake-link', 'intake-submission', 'owner-prefs',"
    " 'merge', 'remove-library-video', 'remove-research-report')"
)


def _set_kind(values: str) -> None:
    op.execute("ALTER TABLE app.proposals DROP CONSTRAINT proposals_kind_check")
    op.execute(
        f"ALTER TABLE app.proposals ADD CONSTRAINT proposals_kind_check CHECK (kind IN {values})"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.owner_prefs (
            principal_id text PRIMARY KEY,
            content text NOT NULL DEFAULT '',
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE app.owner_prefs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.owner_prefs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY owner_prefs_owner ON app.owner_prefs
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.owner_prefs TO jbrain_app")
    _set_kind(_KIND_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM app.proposals WHERE kind = 'owner-prefs'")
    _set_kind(_KIND_OLD)
    op.execute("DROP TABLE IF EXISTS app.owner_prefs")
