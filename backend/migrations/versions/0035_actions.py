"""The actions registry table (workflow engine Phase 5, W0.1).

The six shipped job handlers described as data: one global reference row per
registered action, carrying the handler dispatch key plus the metadata the engine
reasons over (mutating, cost_class, the dedup hint) without running code. The
in-code `jbrain.workflow.registry` is the source of truth and validates at boot
(E3, docs/WORKFLOW_ENGINE_PLAN.md §2/§3); this table is its reference projection so
pipeline/trigger rows can reference an action by name+version.

Reference data like app.canonical_predicates (0031): every principal reads, only
the owner/system context writes — actions are global machinery, not domain content,
so there is no domain_code column and no per-domain firewall on the row itself.

Revision ID: 0035
Revises: 0034
Create Date: 2026-06-15
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.actions (
            name text PRIMARY KEY,
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            -- the app.jobs.kind the worker dispatches to; the registry proves a
            -- real handler exists for it at boot.
            handler text NOT NULL,
            params_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
            -- the cross-domain ingest/integration pipelines run without a narrowed
            -- domain scope; false would pin an action to a domain (E1/E2).
            domain_optional boolean NOT NULL DEFAULT true,
            mutating boolean NOT NULL DEFAULT true,
            cost_class text NOT NULL DEFAULT 'standard'
                CHECK (cost_class IN ('cheap', 'standard', 'expensive')),
            -- optional payload field that makes a re-enqueue idempotent (the
            -- existing has_active dedup, E4); advisory metadata.
            dedup_key_expr text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute("ALTER TABLE app.actions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.actions FORCE ROW LEVEL SECURITY")
    # Global reference data: every principal reads (app.canonical_predicates / app.domains).
    op.execute("CREATE POLICY actions_read ON app.actions FOR SELECT USING (true)")
    # Self-extending machinery: only the owner/system context seeds/edits actions.
    op.execute("CREATE POLICY actions_insert ON app.actions FOR INSERT WITH CHECK (app.is_owner())")
    op.execute(
        "CREATE POLICY actions_update ON app.actions"
        " FOR UPDATE USING (app.is_owner()) WITH CHECK (app.is_owner())"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.actions TO jbrain_app")

    # Seed the six shipped actions (mirrors jbrain.workflow.registry.ACTION_SPECS;
    # the registry's boot validation keeps the two in lockstep).
    op.execute(
        """
        INSERT INTO app.actions (name, version, handler, mutating, cost_class, dedup_key_expr)
        VALUES
            ('ingest_note', 1, 'ingest_note', true, 'standard', 'note_id'),
            ('embed_note', 1, 'embed_note', true, 'standard', 'note_id'),
            ('integrate_note', 1, 'integrate_note', true, 'expensive', 'note_id'),
            ('ocr_attachment', 1, 'ocr_attachment', true, 'expensive', 'attachment_id'),
            ('consolidate_predicates', 1, 'consolidate_predicates', true, 'standard', NULL),
            ('sync_predicates', 1, 'sync_predicates', true, 'standard', NULL)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE app.actions")
