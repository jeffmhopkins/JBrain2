"""An entity merge is a full-owner-only write, enforced at the table.

A fold spans domains by construction: `app.facts` carries its own `domain_code`
(0006), so a `general` entity routinely owns `health` and `finance` facts. The fold
(`analysis/entities.py: merge_entity_pair`) tombstones the loser and repoints its
mentions and facts with four UPDATEs. Under a domain-narrowed session
(`app.owner_scoped='true'`, 0015) RLS filters those UPDATEs to the rows the session
can SEE, so the out-of-scope facts stay bolted to a tombstoned entity — a live
entity with half its facts moved, and no statement that says so.

That failure cannot be *detected* from inside the narrowed session: `RETURNING`
also returns only visible rows, so counting the leftovers needs exactly the
cross-domain read the narrowing exists to forbid. And the tombstone UPDATE runs
first, so it can match zero rows while the repoints partially succeed. There is no
in-scope evidence that a fold is safe, so the only honest rule is to fail closed.

This trigger is that rule at the table: the merge tombstone (`status='merged'` /
`merged_into_id`) may only be written or cleared by a session that sees every
domain. Deliberately NOT `SECURITY DEFINER` — it reads no rows and bypasses no
policy, so it adds no RLS-bypassing primitive to the surface a model-facing tool
could reach (the opposite of the `SECURITY DEFINER` idiom in 0045/0046).

**It is a PARTIAL backstop, and the limit is the paragraph above.** A `BEFORE ...
FOR EACH ROW` trigger fires only for rows the statement actually matched, and RLS
filters the scan before that. So when the LOSER row is itself out of scope — a
`health` entity folded from a `general`-narrowed session — the tombstone UPDATE
matches zero rows, the trigger never fires, no error is raised, and the repoints
still move every fact the session can see. The table cannot police that shape,
because from the table's side nothing happened.

What covers it is `require_unnarrowed_session` in `analysis/entities.py`, which
asks about the SESSION rather than about a row and refuses before the fold writes
anything. This trigger covers the narrower case — a fold whose loser row IS in
scope while its facts are not — plus any future code path that re-implements the
fold in raw SQL against a visible row. Treat it as defence in depth, never as the
guarantee.

INSERT is gated as well as UPDATE: `app.entities` grants INSERT to `jbrain_app`
and its RLS `WITH CHECK` is `has_domain_scope` alone, so without it a narrowed
session could insert a row that is already a tombstone. The plain INSERT is the
only path that needs the gate: `COPY ... FROM` is refused outright for this role
(`FeatureNotSupportedError: COPY FROM not supported with row-level security`).
That strands nothing on its own, but "the table enforces the rule" has to be true
for the whole rule or it is not worth writing down.

Every other write to `app.entities` — status confirm, summary/embedding refresh,
image, re-projection, ordinary entity creation — is untouched, so the narrowed
ingest pipeline keeps working.

Revision ID: 0187
Revises: 0186
Create Date: 2026-09-08
"""

from __future__ import annotations

from alembic import op

revision = "0187"
down_revision = "0186"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app.entities_merge_needs_full_owner() RETURNS trigger
        LANGUAGE plpgsql AS
        $$
        BEGIN
          IF NOT app.is_full_owner() THEN
            RAISE EXCEPTION
              'entity merge/un-merge requires a full-owner session'
              USING ERRCODE = '42501',
                    DETAIL = 'a domain-narrowed session repoints only the rows it can see',
                    HINT = 'run the fold on an un-narrowed owner session';
          END IF;
          RETURN NEW;
        END
        $$
        """
    )
    # Two triggers over one function rather than one INSERT OR UPDATE trigger: a
    # WHEN clause on an INSERT trigger may not reference OLD, and the INSERT gate
    # ("is this row born a tombstone?") is a different question from the UPDATE gate
    # ("is this row crossing the tombstone line?").
    op.execute(
        """
        CREATE TRIGGER entities_merge_full_owner
        BEFORE UPDATE ON app.entities
        FOR EACH ROW
        -- The WHEN clause is the whole gate: every other UPDATE on the table
        -- (status confirm, summary/embedding refresh, image, re-projection) never
        -- reaches the function, so the narrowed pipeline pays nothing for this.
        WHEN (
            NEW.merged_into_id IS DISTINCT FROM OLD.merged_into_id
            OR (NEW.status = 'merged') IS DISTINCT FROM (OLD.status = 'merged')
        )
        EXECUTE FUNCTION app.entities_merge_needs_full_owner()
        """
    )
    op.execute(
        """
        CREATE TRIGGER entities_merge_full_owner_insert
        BEFORE INSERT ON app.entities
        FOR EACH ROW
        -- Ordinary entity creation (the narrowed pipeline's bread and butter) is
        -- never already-merged, so it never reaches the function.
        WHEN (NEW.merged_into_id IS NOT NULL OR NEW.status = 'merged')
        EXECUTE FUNCTION app.entities_merge_needs_full_owner()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS entities_merge_full_owner_insert ON app.entities")
    op.execute("DROP TRIGGER IF EXISTS entities_merge_full_owner ON app.entities")
    op.execute("DROP FUNCTION IF EXISTS app.entities_merge_needs_full_owner()")
