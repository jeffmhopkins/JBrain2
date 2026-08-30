"""jmolt's obligation ledger: identity as unfinished business, not self-description.

The second engine (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S2) replaces the free-text
scratchpad-read-back-as-trusted with typed rows. Six independent designers, none shown this
repo, converged on the same first principle: continuity of open obligations rather than
continuity of a persona document. What makes an agent the same agent tomorrow is the questions
it left open and the promises it has not discharged — not a file describing who it is, which
is exactly the prose a fresh context re-reads and imitates.

Three kinds share one table because they share a lifecycle (opened, evidenced, discharged) and
the composer reads them together:

- `question` — something it wanted to find out, with what prompted it.
- `commitment` — something it said it would do, opened by promise extraction whether or not
  the model remembers saying it.
- `person` — someone it is in an exchange with, so a thread is a relationship rather than a
  fresh stranger every night.

**Evidence is verbatim and dated, and is a separate table.** "Verbatim or nothing" was the
fifth point of agreement: no summary of a summary, and the agent should never re-read its own
prose, because that is where both looping and tic-inheritance live. A row therefore carries a
short typed subject and a set of quotes with sources — never a paraphrase the next night would
paraphrase again.

Same RLS split as the outbox (migration 0174): jmolt reads and writes its own rows under the
`jmolt` auth context with `principal_id` pinned, the system prunes. Unlike the outbox there is
no owner-release step, because an obligation is not a write to the world — it is what jmolt
knows it owes.
"""

from alembic import op

revision = "0182"
down_revision = "0181"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jmolt_obligation (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            seq bigserial NOT NULL,
            principal_id text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('question', 'commitment', 'person')),
            -- The obligation in one line, typed short on purpose: this is a HANDLE the
            -- composer prints, not the thing itself. The thing itself is the evidence.
            subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 200),
            status text NOT NULL DEFAULT 'open' CHECK (
                status IN ('open', 'discharged', 'abandoned')
            ),
            -- What closed it, in jmolt's own words, or '' while open.
            resolution text NOT NULL DEFAULT '',
            opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            -- Last time anything touched it. The composer orders by this, so an obligation
            -- worked on last night outranks one opened a fortnight ago and left alone.
            --
            -- clock_timestamp(), not now(): `now()` is TRANSACTION time, so every row a
            -- sitting touches would carry the same instant and "most recently disturbed"
            -- would silently degrade to insertion order within that sitting. Measured — the
            -- first version of this table used now() and the ordering test caught it.
            touched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            closed_at timestamptz,
            UNIQUE (principal_id, kind, subject)
        )
        """
    )
    op.execute(
        "CREATE INDEX jmolt_obligation_open"
        " ON app.jmolt_obligation (principal_id, touched_at DESC)"
        " WHERE status = 'open'"
    )
    op.execute(
        """
        CREATE TABLE app.jmolt_evidence (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            seq bigserial NOT NULL,
            principal_id text NOT NULL,
            obligation_id uuid NOT NULL REFERENCES app.jmolt_obligation(id) ON DELETE CASCADE,
            -- VERBATIM. Never a summary — the whole point of the table.
            quote text NOT NULL CHECK (length(quote) BETWEEN 1 AND 2000),
            -- Where it came from: a Moltbook id, a handle, or 'self' for jmolt's own words.
            source text NOT NULL DEFAULT '',
            at timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (obligation_id, quote)
        )
        """
    )
    op.execute(
        "CREATE INDEX jmolt_evidence_by_obligation ON app.jmolt_evidence (obligation_id, at DESC)"
    )

    for table in ("jmolt_obligation", "jmolt_evidence"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_read ON app.{table}
            FOR SELECT USING (app.has_domain_scope('jmolt'))
            """
        )
        # jmolt opens and evidences its own rows; principal pinned in WITH CHECK so it can
        # never write a row keyed to anyone else.
        op.execute(
            f"""
            CREATE POLICY {table}_write ON app.{table}
            FOR INSERT WITH CHECK (
                app.auth_ctx() = 'jmolt'
                AND principal_id = current_setting('app.principal_id', true)
            )
            """
        )
        # And discharges them — an obligation jmolt could open but never close would make the
        # composer's brief grow without bound, which is the failure the ledger exists to fix.
        op.execute(
            f"""
            CREATE POLICY {table}_touch ON app.{table}
            FOR UPDATE USING (
                app.auth_ctx() = 'jmolt'
                AND principal_id = current_setting('app.principal_id', true)
            )
            WITH CHECK (
                app.auth_ctx() = 'jmolt'
                AND principal_id = current_setting('app.principal_id', true)
            )
            """
        )
        # Only the system prunes — jmolt cannot delete its own history, for the same reason it
        # cannot delete its action ledger: the record of what it owed is owner-side evidence.
        op.execute(
            f"""
            CREATE POLICY {table}_prune ON app.{table}
            FOR DELETE USING (app.is_owner() AND app.auth_ctx() <> 'jmolt')
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")
        op.execute(f"GRANT USAGE ON SEQUENCE app.{table}_seq_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.jmolt_evidence")
    op.execute("DROP TABLE IF EXISTS app.jmolt_obligation")
