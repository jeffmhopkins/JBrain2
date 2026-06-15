"""Phase-5 workflow-engine foundation tables (docs/WORKFLOW_ENGINE_PLAN.md §3).

The data-model substrate the Wave-1 tracks build against: the append-only event
log, the trigger/pipeline/schedule definitions that bind events to actions, the
persisted resolution pins, the stored eval runs the promotion gate reconstructs
from, and reversible `skills` groundwork. Every table is created with full RLS
(ENABLE + FORCE + policy + grant to jbrain_app, CLAUDE.md rule 3) and an isolation
test (tests/integration/test_workflow_tables_rls.py).

Two RLS postures, both established precedents:

- **Domain-firewalled** (`events`, `resolution_pin`, `skills`) carry their own
  `domain_code` and use the `app.has_domain_scope(domain_code)` policy from 0006:
  the event's fail-closed stamp (E2), the pin's note domain (it cascades with the
  note, N15), and the skill's domain.
- **Owner/system** (`pipelines`, `triggers`, `schedules`, `eval_runs`) hold
  definition/audit metadata with no note content, so they use the `app.is_owner()`
  policy from 0016 (`agent_runs`) / `app.jobs` (0003). `pipelines` is reference
  data and follows the `canonical_predicates` (0031) global-read + owner-write
  split so any narrowed reader can resolve an action ref while only the owner/
  system context edits a definition.

NOT created here: `runs`/`run_steps` and `actions`. `runs`/`run_steps` come from
the in-place `agent_runs` rename in Wave-1 Track A (§3); creating them here would
double-create. `actions` is the sibling W0.1 action-registry task.

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-15
"""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- pipelines: stored definitions; global-read reference data (0031) ----
    # An ordered set of action refs; ingest + integration become two of these in
    # Wave 2 (E7). Linear first; DAG deferred (§7). name+version is the address a
    # trigger references (E3); a definition change is a new version, never an edit.
    op.execute(
        """
        CREATE TABLE app.pipelines (
            name text NOT NULL,
            version integer NOT NULL DEFAULT 1,
            steps jsonb NOT NULL DEFAULT '[]',
            description text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (name, version)
        )
        """
    )

    # --- schedules: scheduler claim targets; owner/system config (0016) -------
    # interval + explicit next_run_at (no cron parser dep, §7); the tick advances
    # next_run_at app-side so a fake clock controls it (N3). The scheduler claims
    # by next_run_at SKIP LOCKED, designed so a second worker is safe later.
    op.execute(
        """
        CREATE TABLE app.schedules (
            id uuid PRIMARY KEY,
            interval_seconds integer NOT NULL CHECK (interval_seconds > 0),
            timezone text NOT NULL DEFAULT 'UTC',
            next_run_at timestamptz NOT NULL,
            last_run_at timestamptz,
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX schedules_due_idx ON app.schedules (next_run_at) WHERE enabled")

    # --- triggers: bind an event type OR a schedule to a pipeline; owner config -
    # Exactly one source: on_event (an event type) or on_schedule_id. manual=true
    # marks an emergency-fireable sweep (a "run now" Ops control). filter is the
    # conjunctive TriggerFilter (workflow/contracts.py). on_event and pipeline are
    # free text by design (no FK): event "types" have no table, and pipeline binds a
    # name across versions (pipelines PK is composite (name, version)).
    op.execute(
        """
        CREATE TABLE app.triggers (
            id uuid PRIMARY KEY,
            on_event text,
            on_schedule_id uuid REFERENCES app.schedules(id) ON DELETE CASCADE,
            pipeline text NOT NULL,
            filter jsonb NOT NULL DEFAULT '{}',
            enabled boolean NOT NULL DEFAULT true,
            manual boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            -- Exactly one source binds the trigger (event type xor schedule).
            CHECK ((on_event IS NULL) <> (on_schedule_id IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX triggers_event_idx ON app.triggers (on_event) WHERE enabled")

    # --- events: append-only event log; domain-firewalled (0006) -------------
    # domain_code is the fail-closed stamp (E2, most-restrictive scope the
    # triggering content touched); principal_id is the triggering identity the
    # dispatcher narrows a SessionContext from (E1). dispatched_at NULL until
    # the dispatcher has fanned it out (the claim target for fan-out).
    op.execute(
        """
        CREATE TABLE app.events (
            id uuid PRIMARY KEY,
            type text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}',
            domain_code text NOT NULL REFERENCES app.domains(code),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            occurred_at timestamptz NOT NULL DEFAULT now(),
            dispatched_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX events_undispatched_idx ON app.events (occurred_at)"
        " WHERE dispatched_at IS NULL"
    )

    # --- resolution_pin: persists analysis/pins.py ResolutionPin; firewalled --
    # PK is (note_id, chunk_id, occurrence_index, decision_kind): occurrence_index
    # is CHUNK-relative, so chunk_id MUST be in the key or a note-only key collides
    # across chunks (the pins.py docstring's explicit A8 warning). Cascade-purged
    # with the note (N15). Exactly one of entity_id / normalized_predicate is set,
    # per decision_kind. domain_code carries the note's domain (the pin is
    # note-derived plaintext behind the same firewall).
    op.execute(
        """
        CREATE TABLE app.resolution_pin (
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            chunk_id uuid NOT NULL REFERENCES app.chunks(id) ON DELETE CASCADE,
            occurrence_index integer NOT NULL,
            decision_kind text NOT NULL
                CHECK (decision_kind IN ('identity', 'predicate_key')),
            surface text NOT NULL,
            span_text_hash text NOT NULL,
            entity_id uuid REFERENCES app.entities(id) ON DELETE CASCADE,
            normalized_predicate text,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (note_id, chunk_id, occurrence_index, decision_kind),
            -- Exactly one decision payload, matching decision_kind (pins.py).
            CHECK (
                (decision_kind = 'identity'
                    AND entity_id IS NOT NULL AND normalized_predicate IS NULL)
                OR (decision_kind = 'predicate_key'
                    AND normalized_predicate IS NOT NULL AND entity_id IS NULL)
            )
        )
        """
    )

    # --- eval_runs: stored eval results; owner/system audit (0016) -----------
    # scores is jsonb holding the per-fixture {fixture, task, safety} split so
    # promotion_decision reconstructs EvalRun/FixtureScore candidate<->baseline
    # WITHOUT collapsing the two-dimensional gate (a flat blob would defeat it).
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

    # --- skills: reversible groundwork; domain-firewalled (no consumer yet) ---
    # The Phase-6 skill substrate stood up reversibly now (no promotion logic this
    # phase, I-5/I-6); skill_version stamps on runs land with Track C. embedding
    # mirrors entities.summary_embedding (0006) for later semantic recall.
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

    # --- RLS: domain-firewalled tables (app.has_domain_scope, 0006) -----------
    for table in ("events", "resolution_pin", "skills"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_domain ON app.{table}
            USING (app.has_domain_scope(domain_code))
            WITH CHECK (app.has_domain_scope(domain_code))
            """
        )

    # --- RLS: owner/system definition + audit tables (app.is_owner(), 0016) ---
    for table in ("triggers", "schedules", "eval_runs"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_owner ON app.{table}
            USING (app.is_owner())
            WITH CHECK (app.is_owner())
            """
        )

    # --- RLS: pipelines is global-read reference data (canonical_predicates, 0031) -
    # A narrowed reader resolves an action ref it must execute; only the owner/
    # system context defines or revises a pipeline.
    op.execute("ALTER TABLE app.pipelines ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.pipelines FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY pipelines_read ON app.pipelines FOR SELECT USING (true)")
    op.execute(
        "CREATE POLICY pipelines_insert ON app.pipelines FOR INSERT WITH CHECK (app.is_owner())"
    )
    op.execute(
        "CREATE POLICY pipelines_update ON app.pipelines"
        " FOR UPDATE USING (app.is_owner()) WITH CHECK (app.is_owner())"
    )

    # --- grants ---------------------------------------------------------------
    # events: append-only log, plus UPDATE to stamp dispatched_at; never DELETE.
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.events TO jbrain_app")
    # triggers/schedules: owner config — full CRUD incl. DELETE (a definition can
    # be removed, unlike append-only audit rows).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.triggers TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.schedules TO jbrain_app")
    # pipelines: reference definitions — new versions insert, edits update; the
    # version key means a definition is superseded, not deleted.
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.pipelines TO jbrain_app")
    # resolution_pin: re-integration rebuilds a note's pins wholesale (DELETE +
    # INSERT), the chunks/mentions rebuild pattern (0003/0006); UPDATE re-decides
    # an existing pin in place.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.resolution_pin TO jbrain_app")
    # eval_runs: stored results — append-only audit; never edited or deleted.
    op.execute("GRANT SELECT, INSERT ON app.eval_runs TO jbrain_app")
    # skills: groundwork — full CRUD incl. DELETE for reversibility (no consumer
    # this wave; Phase-6 promotion logic lands later).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.skills TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.skills")
    op.execute("DROP TABLE app.eval_runs")
    op.execute("DROP TABLE app.resolution_pin")
    op.execute("DROP TABLE app.events")
    op.execute("DROP TABLE app.triggers")
    op.execute("DROP TABLE app.schedules")
    op.execute("DROP TABLE app.pipelines")
