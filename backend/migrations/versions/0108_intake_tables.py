"""Guided-intake share-link tables: links, sessions, submissions (W1).

Three owner-owned tables plus a NON-owner read path pinned to the per-session
intake principal (migration 0107). The security spine (GUIDED_INTAKE_PLAN.md §5):

  * `intake_links` — the durable link: its config, run/open caps, and the
    show-once `secret_hash` (#14, only a hash is ever stored). Owner-managed; the
    redeem path reads/updates it under the `login`/`bootstrap` auth contexts
    (no principal exists yet), exactly as `principals`/`device_sessions` do.

  * `intake_sessions` — one NON-owner session row per redeem (NOT the owner-only
    `app.agent_sessions`, whose `USING(app.is_owner())` policy would reject this
    principal anyway). Carries the per-session `principal_id` — the isolation pin
    — and a `config_snapshot` so a later edit to the link never alters a session
    already opened.

  * `intake_submissions` — the captured submission + full transcript, surfaced to
    the owner as a Proposal (W4). The capturing recipient (W3) writes only its own
    row; the redundant `principal_id` is the pin so the RLS policy never has to
    reach across to `intake_sessions`.

Isolation model — why `principal_id`, not a domain/subject GUC pin:
The intake principal runs with an EMPTY scope (`jbrain.db.session.intake_context`:
no `domain_scopes`, no `subject_id`), so `app.has_domain_scope()` is false for
every domain and it reads zero notes/chunks/locations (#8). Row access on these
three tables is therefore granted by the per-session `principal_id` alone, with
`app.is_full_owner()` as the owner escape (never the `is_owner()` shortcut, §5).
`subject_id`/`domain_code` on `intake_links` are ATTRIBUTION metadata (which
subject/domain an approved submission lands under, read owner-side at approval),
never a read grant to the stranger.
"""

from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.intake_links (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Attribution (pinned per link, #9): which subject/domain an approved
            -- submission lands under. NOT a read grant — the stranger never queries
            -- under this subject/domain (see the module docstring).
            subject_id uuid NOT NULL REFERENCES app.subjects(id),
            domain_code text NOT NULL REFERENCES app.domains(code),
            label text NOT NULL DEFAULT '',
            -- The persona's fixed frame vs. the per-link "what to collect": both are
            -- templated into the intake persona prompt as DATA (W2), never as policy.
            persona_brief text NOT NULL DEFAULT '',
            fields_brief text NOT NULL DEFAULT '',
            opening_blurb text NOT NULL DEFAULT '',
            -- Two independent ceilings (§7): opens burn at redeem, runs at submission.
            -- The link dies when either caps, on TTL (`expires_at`), or on revoke.
            max_runs int NOT NULL CHECK (max_runs > 0),
            runs_used int NOT NULL DEFAULT 0 CHECK (runs_used >= 0),
            max_opens int NOT NULL CHECK (max_opens > 0),
            opens_used int NOT NULL DEFAULT 0 CHECK (opens_used >= 0),
            -- bind-on-first (one person) caps effective opens at 1; open (multi-person)
            -- caps at max_opens. One atomic counter enforces both (the repo's claim).
            bind_on_first boolean NOT NULL,
            capture_enterer_name boolean NOT NULL DEFAULT true,
            disclose_owner_identity boolean NOT NULL DEFAULT false,
            -- Show-once: only the SHA-256 of the secret is stored (#14). To re-send a
            -- link, re-mint — there is no path back to the plaintext.
            secret_hash text NOT NULL UNIQUE,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked', 'exhausted')),
            created_at timestamptz NOT NULL DEFAULT now(),
            -- TTL: the redeem gate and the per-session principal's expiry both enforce
            -- it, so a cookie cannot outlive the link's box.
            expires_at timestamptz NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app.intake_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            link_id uuid NOT NULL REFERENCES app.intake_links(id) ON DELETE CASCADE,
            opened_at timestamptz NOT NULL DEFAULT now(),
            -- The link config frozen at open: a live edit to the link affects only
            -- sessions opened afterward.
            config_snapshot jsonb NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'drafting'
                CHECK (status IN ('drafting', 'submitted', 'abandoned'))
        )
        """
    )
    op.execute(
        "CREATE INDEX intake_sessions_link_idx ON app.intake_sessions (link_id, opened_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE app.intake_submissions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            link_id uuid NOT NULL REFERENCES app.intake_links(id) ON DELETE CASCADE,
            session_id uuid NOT NULL REFERENCES app.intake_sessions(id) ON DELETE CASCADE,
            -- The per-session principal, denormalized from the session so the RLS pin
            -- is a direct column compare (no cross-table subquery in the policy).
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            -- Untrusted: a name the stranger typed. Never an authz input (#9).
            enterer_name text NOT NULL DEFAULT '',
            transcript jsonb NOT NULL DEFAULT '[]',
            draft jsonb NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'submitted'
                CHECK (status IN (
                    'drafting', 'submitted', 'abandoned', 'proposed', 'landed', 'rejected'
                )),
            proposal_id uuid REFERENCES app.proposals(id),
            note_ids uuid[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX intake_submissions_link_idx"
        " ON app.intake_submissions (link_id, created_at DESC)"
    )

    for table in ("intake_links", "intake_sessions", "intake_submissions"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")

    # intake_links: owner-managed (full owner, never the is_owner() shortcut — §5), with
    # the auth-context carve-out so the redeem flow can read the link by secret_hash and
    # atomically bump opens_used before any principal context exists (mirrors principals).
    op.execute(
        """
        CREATE POLICY intake_links_select ON app.intake_links FOR SELECT
        USING (app.is_full_owner() OR app.auth_ctx() IN ('login', 'bootstrap'))
        """
    )
    op.execute(
        """
        CREATE POLICY intake_links_insert ON app.intake_links FOR INSERT
        WITH CHECK (app.is_full_owner() OR app.auth_ctx() = 'bootstrap')
        """
    )
    op.execute(
        """
        CREATE POLICY intake_links_update ON app.intake_links FOR UPDATE
        USING (app.is_full_owner() OR app.auth_ctx() = 'bootstrap')
        WITH CHECK (app.is_full_owner() OR app.auth_ctx() = 'bootstrap')
        """
    )

    # intake_sessions: the per-session principal sees/updates ONLY its own row (the
    # principal_id pin); the full owner sees all; redeem inserts under bootstrap.
    op.execute(
        """
        CREATE POLICY intake_sessions_select ON app.intake_sessions FOR SELECT
        USING (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
            OR app.auth_ctx() IN ('login', 'bootstrap')
        )
        """
    )
    op.execute(
        """
        CREATE POLICY intake_sessions_insert ON app.intake_sessions FOR INSERT
        WITH CHECK (app.is_full_owner() OR app.auth_ctx() = 'bootstrap')
        """
    )
    op.execute(
        """
        CREATE POLICY intake_sessions_update ON app.intake_sessions FOR UPDATE
        USING (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
            OR app.auth_ctx() = 'bootstrap'
        )
        WITH CHECK (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
            OR app.auth_ctx() = 'bootstrap'
        )
        """
    )

    # intake_submissions: same per-session pin. The capturing recipient (W3) writes only
    # a row carrying its OWN principal_id (the WITH CHECK forbids forging another's).
    op.execute(
        """
        CREATE POLICY intake_submissions_select ON app.intake_submissions FOR SELECT
        USING (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
        )
        """
    )
    op.execute(
        """
        CREATE POLICY intake_submissions_insert ON app.intake_submissions FOR INSERT
        WITH CHECK (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
        )
        """
    )
    op.execute(
        """
        CREATE POLICY intake_submissions_update ON app.intake_submissions FOR UPDATE
        USING (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
        )
        WITH CHECK (
            app.is_full_owner()
            OR principal_id::text = current_setting('app.principal_id', true)
        )
        """
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.intake_links TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.intake_sessions TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.intake_submissions TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.intake_submissions")
    op.execute("DROP TABLE app.intake_sessions")
    op.execute("DROP TABLE app.intake_links")
