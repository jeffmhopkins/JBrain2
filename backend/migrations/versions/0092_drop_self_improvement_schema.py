"""Schema teardown for the self-improvement removal (Wave 3).

Waves 1-2 removed all self-improvement code (the eval/promotion harness, the
Loop 2-4 actions, the `skills` consumers) and Wave 1 deregistered its nightly
seeds (0091). The now-unused DB schema still remained: the `app.skills` and
`app.eval_runs` tables (0036), the `app.runs.skill_version` column (0043), and
the `'prompt-edit'` / `'skill-promotion'` proposal kinds (0018/0027/0057). This
migration drops them, leaving the workflow engine's surviving tables intact.

`upgrade()` runs in FK-safe order: it first deletes any stranded proposals of
the removed kinds (and their `proposal_nodes` children — the only child table of
`app.proposals`, ON DELETE CASCADE per 0018) so tightening the kind CHECK can't
fail on an existing row, then tightens `proposals_kind_check`, drops the
`skill_version` column, and drops the two tables. No other table carries a FK
into `app.skills` or `app.eval_runs` (verified against the migration history), so
the drops need no further cascade handling.

`downgrade()` restores the prior schema exactly, in reverse order: it recreates
`app.eval_runs` and `app.skills` verbatim from 0036 (every index, RLS
enable/force, policy, and grant), re-adds `app.runs.skill_version` (0043), and
restores `proposals_kind_check` to the 0057 set. It restores SCHEMA, not data —
the proposal rows `upgrade()` deleted are not resurrected (downgrade is a
structural reversal, the same posture as any DROP TABLE migration here).

Revision ID: 0092
Revises: 0091
Create Date: 2026-06-24
"""

from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

# The proposal kinds the self-improvement removal retires.
_REMOVED_KINDS = "('prompt-edit', 'skill-promotion')"

# The kind set after removal (the 0057 set minus prompt-edit + skill-promotion).
_NEW_KINDS = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'predicate-canon', 'egress')"
)
# The prior set restored on downgrade (verbatim 0057 _NEW).
_OLD_KINDS = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'predicate-canon', 'egress')"
)


def _set_kind_check(values: str) -> None:
    op.execute("ALTER TABLE app.proposals DROP CONSTRAINT proposals_kind_check")
    op.execute(
        f"ALTER TABLE app.proposals ADD CONSTRAINT proposals_kind_check CHECK (kind IN {values})"
    )


def upgrade() -> None:
    # 1. Purge stranded proposals of the removed kinds before tightening the CHECK.
    # proposal_nodes (0018) is the only child of proposals; its FK is ON DELETE
    # CASCADE, so a plain DELETE on the parent would suffice, but we delete the
    # children explicitly first to keep the order obvious and FK-safe.
    op.execute(
        "DELETE FROM app.proposal_nodes WHERE proposal_id IN"
        f" (SELECT id FROM app.proposals WHERE kind IN {_REMOVED_KINDS})"
    )
    op.execute(f"DELETE FROM app.proposals WHERE kind IN {_REMOVED_KINDS}")

    # 2. Tighten the kind CHECK to the post-removal set.
    _set_kind_check(_NEW_KINDS)

    # 3. Drop the skill-promotion audit column on runs (0043).
    op.execute("ALTER TABLE app.runs DROP COLUMN skill_version")

    # 4. Drop the two now-unused tables (no FK references either, verified).
    op.execute("DROP TABLE app.skills")
    op.execute("DROP TABLE app.eval_runs")


def downgrade() -> None:
    # Recreate the schema exactly as it stood before 0092 (reverse order).

    # --- eval_runs: stored eval results; owner/system audit (0016), from 0036 ---
    op.execute(
        """
        CREATE TABLE app.eval_runs (
            id uuid PRIMARY KEY,
            suite text NOT NULL,
            version_label text NOT NULL,
            model text NOT NULL,
            new_case text,
            scores jsonb NOT NULL DEFAULT '[]',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX eval_runs_suite_idx ON app.eval_runs (suite, created_at DESC)")

    # --- skills: reversible groundwork; domain-firewalled (0036) ---------------
    op.execute(
        """
        CREATE TABLE app.skills (
            id uuid PRIMARY KEY,
            name text NOT NULL,
            version integer NOT NULL DEFAULT 1,
            status text NOT NULL DEFAULT 'shadow'
                CHECK (status IN ('shadow', 'active', 'quarantined')),
            domain_code text NOT NULL REFERENCES app.domains(code),
            body text NOT NULL DEFAULT '',
            description text NOT NULL DEFAULT '',
            embedding vector(384),
            embedding_model text,
            success_stats jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (name, version)
        )
        """
    )
    op.execute(
        "CREATE INDEX skills_embedding_idx ON app.skills USING hnsw (embedding vector_cosine_ops)"
    )

    # eval_runs: owner/system audit RLS (app.is_owner(), 0016).
    op.execute("ALTER TABLE app.eval_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.eval_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY eval_runs_owner ON app.eval_runs
        USING (app.is_owner())
        WITH CHECK (app.is_owner())
        """
    )

    # skills: domain-firewalled RLS (app.has_domain_scope, 0006).
    op.execute("ALTER TABLE app.skills ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.skills FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY skills_domain ON app.skills
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )

    # Grants (verbatim 0036).
    op.execute("GRANT SELECT, INSERT ON app.eval_runs TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.skills TO jbrain_app")

    # --- runs.skill_version: re-add the audit column (0043) -------------------
    op.execute("ALTER TABLE app.runs ADD COLUMN skill_version text")

    # --- proposals_kind_check: restore the prior (0057) kind set --------------
    _set_kind_check(_OLD_KINDS)
