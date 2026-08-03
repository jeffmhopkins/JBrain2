"""Public share links for jlaunch runs: a revocable, no-login read grant.

The owner mints a link targeting ONE run (`app.jlaunch_runs`, 0152); anyone with the link
reads that run's public results page + downloads its artifact (a plain token-in-URL bearer
read — only the token's SHA-256 hash is stored). The owner revokes it (`revoked_at`) to kill
it. No cookie, no session. Mirrors the research report share (0150).

The capability is enforced in RLS, not app code. A public visitor runs under a non-owner,
EMPTY-scope context (`jlaunch_share_context`, db/session.py) carrying
`auth_context = 'jlaunch_share'` and, once the token resolves, `principal_id = <link id>` as
the pin. The ONLY thing that lets a visitor read a run is `jlaunch_runs_share` below, which
joins the pinned link to the run and re-checks `revoked_at` — so a visitor pinned to link A
reads exactly link A's run, and a revoked link reads nothing, at the data layer regardless of
the SQL the endpoint runs. (The artifact bytes themselves live in the content-addressed blob
store, addressed by the sha this run row carries — no RLS needed on blobs.)

Revision ID: 0153
Revises: 0152
Create Date: 2026-08-03
"""

from alembic import op

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jlaunch_share_links (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Only the SHA-256 of the 256-bit bearer secret; the secret is shown once at
            -- mint and never recoverable.
            token_hash text NOT NULL UNIQUE,
            label text,
            -- The run id is the control server's hex token (text), not a uuid.
            run_id text NOT NULL REFERENCES app.jlaunch_runs(id) ON DELETE CASCADE,
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            last_viewed_at timestamptz,
            view_count integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute("CREATE INDEX jlaunch_share_links_run_idx ON app.jlaunch_share_links (run_id)")
    op.execute("ALTER TABLE app.jlaunch_share_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jlaunch_share_links FORCE ROW LEVEL SECURITY")
    # Owner owns the link lifecycle (mint / list / revoke); the SYSTEM view-count bump runs
    # as owner too. A public visitor may only SELECT (resolve a token, and satisfy the
    # runs-policy subquery) — never write.
    op.execute(
        """
        CREATE POLICY jlaunch_share_links_owner ON app.jlaunch_share_links
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        """
        CREATE POLICY jlaunch_share_links_public_read ON app.jlaunch_share_links
        FOR SELECT USING (app.auth_ctx() = 'jlaunch_share')
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.jlaunch_share_links TO jbrain_app")

    # The keystone: the ONLY grant a share visitor gets on the run index. Additive and
    # permissive (OR'd with jlaunch_runs_owner), but the owner policy denies a non-owner, so
    # for the share context this is the sole SELECT path. It matches the pinned link by id,
    # re-checks revocation, and joins the link's run_id to the run. The link id is compared as
    # TEXT, never casting the `app.principal_id` GUC to uuid: a SELECT policy is also evaluated
    # on INSERT ... RETURNING rows and Postgres constant-folds a `::uuid` cast, so a non-uuid
    # principal would throw at plan time. Text compare fails closed instead — an empty/non-uuid
    # pin simply matches no link.
    op.execute(
        """
        CREATE POLICY jlaunch_runs_share ON app.jlaunch_runs FOR SELECT
        USING (
            app.auth_ctx() = 'jlaunch_share'
            AND EXISTS (
                SELECT 1 FROM app.jlaunch_share_links l
                WHERE l.id::text = current_setting('app.principal_id', true)
                  AND l.revoked_at IS NULL
                  AND l.run_id = app.jlaunch_runs.id
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY jlaunch_runs_share ON app.jlaunch_runs")
    op.execute("DROP TABLE app.jlaunch_share_links")
