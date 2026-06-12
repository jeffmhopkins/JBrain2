"""Tier-A agent memory: working/behavioral memory, episodic traces, and the
episode→graph pointer table (docs/ASSISTANT.md "Memory model", invariants #2/#4/#11).

Three tables, all owner-only and domain-firewalled in Postgres:

- `agent_memory` — the MD-as-rows working/behavioral store (block_kind core/task/
  self_semantic). Owner-only, narrowed by `app.has_domain_scope(domain_code)`: a
  health-only session cannot read finance memory, and a non-owner principal reads
  none (invariant #8).
- `agent_episodes` — conversation/task traces with a segregated-namespace
  embedding (its own table, so an episode can never be matched as a citable
  chunk). Scoped to the SET of domains the turn touched and gated by the new
  `app.has_all_domain_scopes` firewall: a multi-domain episode is visible only to
  a session holding **all** touched scopes — never decomposed into a `general`
  row (invariant #4, fail-closed).
- `agent_episode_refs` — pointers (note/fact/entity id) back into the cited graph,
  never copies (invariant #2). Visible exactly when their parent episode is (the
  RLS on `agent_episodes` filters the subquery), and they cascade with it — the
  purge target for note deletion (invariant #11).

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-12
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

# Every code in `codes` must be in the session's selected scopes — the fail-closed
# rule for episodic memory (invariant #4). The owner short-circuits unless the
# session opted into narrowing (app.owner_scoped='true'), mirroring has_domain_scope.
_HAS_ALL = """
CREATE OR REPLACE FUNCTION app.has_all_domain_scopes(codes text[]) RETURNS boolean
LANGUAGE sql STABLE AS
$$
  SELECT (
      app.is_owner()
      AND coalesce(current_setting('app.owner_scoped', true), '') <> 'true'
  )
  OR codes <@ string_to_array(coalesce(current_setting('app.domain_scopes', true), ''), ',')
$$
"""


def upgrade() -> None:
    op.execute(_HAS_ALL)

    op.execute(
        """
        CREATE TABLE app.agent_memory (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            subject_id uuid,
            domain_code text NOT NULL REFERENCES app.domains(code),
            block_kind text NOT NULL
                CHECK (block_kind IN ('core', 'task', 'self_semantic')),
            body_md text NOT NULL,
            revision int NOT NULL DEFAULT 1,
            superseded_by uuid REFERENCES app.agent_memory(id),
            source text NOT NULL DEFAULT 'owner_confirmed'
                CHECK (source IN ('owner_confirmed', 'agent_task', 'seed')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX agent_memory_live_idx ON app.agent_memory (domain_code, block_kind)"
        " WHERE superseded_by IS NULL"
    )
    op.execute("ALTER TABLE app.agent_memory ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.agent_memory FORCE ROW LEVEL SECURITY")
    # Owner-only AND domain-narrowed: is_owner() excludes non-owner principals
    # (#8), has_domain_scope narrows the owner to the session's domains (#4).
    op.execute(
        """
        CREATE POLICY agent_memory_owner ON app.agent_memory
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.agent_memory TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.agent_episodes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid REFERENCES app.agent_sessions(id) ON DELETE SET NULL,
            run_id uuid REFERENCES app.agent_runs(id) ON DELETE SET NULL,
            domain_scopes text[] NOT NULL,
            body text NOT NULL,
            importance real NOT NULL DEFAULT 0,
            embedding vector(384),
            embedding_model text,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_accessed_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX agent_episodes_recency_idx ON app.agent_episodes (last_accessed_at DESC)"
    )
    op.execute("ALTER TABLE app.agent_episodes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.agent_episodes FORCE ROW LEVEL SECURITY")
    # Visible only to a session holding ALL touched scopes (#4), owner-only (#8).
    op.execute(
        """
        CREATE POLICY agent_episodes_scopes ON app.agent_episodes
        USING (app.is_owner() AND app.has_all_domain_scopes(domain_scopes))
        WITH CHECK (app.is_owner() AND app.has_all_domain_scopes(domain_scopes))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.agent_episodes TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.agent_episode_refs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            episode_id uuid NOT NULL REFERENCES app.agent_episodes(id) ON DELETE CASCADE,
            note_id uuid REFERENCES app.notes(id) ON DELETE CASCADE,
            fact_id uuid REFERENCES app.facts(id) ON DELETE CASCADE,
            entity_id uuid REFERENCES app.entities(id) ON DELETE CASCADE,
            CHECK (num_nonnulls(note_id, fact_id, entity_id) = 1)
        )
        """
    )
    op.execute("CREATE INDEX agent_episode_refs_episode_idx ON app.agent_episode_refs (episode_id)")
    op.execute("CREATE INDEX agent_episode_refs_note_idx ON app.agent_episode_refs (note_id)")
    op.execute("ALTER TABLE app.agent_episode_refs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.agent_episode_refs FORCE ROW LEVEL SECURITY")
    # A pointer carries no domain content of its own; it is visible exactly when
    # its parent episode is — the agent_episodes RLS filters this subquery.
    op.execute(
        """
        CREATE POLICY agent_episode_refs_via_episode ON app.agent_episode_refs
        USING (EXISTS (SELECT 1 FROM app.agent_episodes e WHERE e.id = episode_id))
        WITH CHECK (EXISTS (SELECT 1 FROM app.agent_episodes e WHERE e.id = episode_id))
        """
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON app.agent_episode_refs TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.agent_episode_refs")
    op.execute("DROP TABLE app.agent_episodes")
    op.execute("DROP TABLE app.agent_memory")
    op.execute("DROP FUNCTION app.has_all_domain_scopes(text[])")
