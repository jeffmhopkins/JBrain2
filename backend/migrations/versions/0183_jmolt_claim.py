"""jmolt's claim ledger: what it has already said, as embedded triples.

The claim gate (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S2) decides what NOT to say by
comparing a candidate against everything jmolt has already claimed. This is where "already
claimed" lives, and it exists because of a measurement rather than a guess.

Pulled from the live box on 2026-08-30: across 2026-08-28 and 2026-08-29 jmolt published six
posts asserting ONE claim — that an owner's prompt is a seed which accumulated context
reshapes — and the sequence CROSSES the night boundary (#58 restates #53 from the night
before). A gate holding only tonight's claims would let the first restatement of every night
through, every night, which is most of the failure.

Two embeddings per row, not one. The gate compares the whole triple to find "the same
territory", then compares the OBJECTS to decide whether a new conclusion was reached — the
supersession exception. Storing both is what stops a night re-embedding its own history to
ask a question it asked yesterday.

Same RLS split as the obligation ledger (0182): jmolt writes its own rows with the principal
pinned, only the system deletes. A claim jmolt could retract is a claim it could repeat.
"""

from alembic import op

revision = "0183"
down_revision = "0182"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jmolt_claim (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            seq bigserial NOT NULL,
            principal_id text NOT NULL,
            subject text NOT NULL,
            predicate text NOT NULL,
            object text NOT NULL,
            -- Where the claim's support came from, for the "cites evidence the prior claim
            -- lacked" exception. An array rather than a join table: it is read whole, always.
            citations text[] NOT NULL DEFAULT '{}',
            -- The triple, and the object alone. 384 to match app.chunks — the same embedder
            -- serves both, and a second dimension would mean a second model to keep alive.
            embedding vector(384),
            object_embedding vector(384),
            -- The outbox row this claim was published as, when it was. NULL for a claim the
            -- gate judged and refused, which is worth keeping: a refusal that keeps happening
            -- is the strongest signal that a threshold is wrong.
            outbox_id uuid,
            published boolean NOT NULL DEFAULT false,
            at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute("CREATE INDEX jmolt_claim_recent ON app.jmolt_claim (principal_id, at DESC)")
    op.execute(
        "CREATE INDEX jmolt_claim_embedding ON app.jmolt_claim"
        " USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("ALTER TABLE app.jmolt_claim ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jmolt_claim FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY jmolt_claim_read ON app.jmolt_claim
        FOR SELECT USING (app.has_domain_scope('jmolt'))
        """
    )
    op.execute(
        """
        CREATE POLICY jmolt_claim_write ON app.jmolt_claim
        FOR INSERT WITH CHECK (
            app.auth_ctx() = 'jmolt'
            AND principal_id = current_setting('app.principal_id', true)
        )
        """
    )
    # No UPDATE policy at all: a claim is what was said, and editing it after the fact would
    # let a night launder a repeat into a novelty.
    op.execute(
        """
        CREATE POLICY jmolt_claim_prune ON app.jmolt_claim
        FOR DELETE USING (app.is_owner() AND app.auth_ctx() <> 'jmolt')
        """
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON app.jmolt_claim TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.jmolt_claim_seq_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.jmolt_claim")
