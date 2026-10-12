"""`panel_debug_request` — a debug token asks a panel something, the panel answers.

docs/plans/JPANEL_C6_PLAN.md §4 (wave C2d). The owner has no terminal and a panel cannot
be reached directly, so the box relays: a debug-token holder queues a command, the panel
picks it up on a short poll and posts the result back. One row per request, short-lived:
queued -> sent -> done, or expired; finished rows are swept after fifteen minutes.

**The panel is a writer here, so the policies have to say what it may write.** A panel
authenticates with a key that lives in flash on a child's wall. It may read only its OWN
pending requests and write only the RESULT of its own requests; it may never create one —
a panel that could queue a command could queue one for its sibling, and a panel that could
answer its sibling's request could hand the owner a forged screenshot of the other room.

RLS decides which rows; it cannot decide which COLUMNS. So a trigger holds the rest for a
`device_key` caller: the request itself (command, args, confirm, who asked) is immutable,
the only transitions are queued -> sent and sent -> done, and the timestamps are the box's,
not the panel's — on pickup the trigger, not the caller, sets the answer deadline, so a
panel cannot keep a request answerable forever by writing a far-future `expires_at`.

`png_sha256` is a blob digest (a decoded screenshot) and is registered in
`jbrain.blob_refs` in the same change, so the sweep never unlinks a file another table
shares.

Revision ID: 0231
Revises: 0230
Create Date: 2026-10-12
"""

from alembic import op

revision = "0231"
down_revision = "0230"
branch_labels = None
depends_on = None

_PANEL_OWN = """
    current_setting('app.principal_kind', true) = 'device_key'
    AND device_id::text = current_setting('app.principal_id', true)
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.panel_debug_request (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id uuid NOT NULL REFERENCES app.principals(id) ON DELETE CASCADE,
            command text NOT NULL CHECK (command ~ '^[a-z_]{1,15}$'),
            args jsonb NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(args) = 'object' AND octet_length(args::text) <= 512),
            confirm boolean NOT NULL DEFAULT false,
            -- The debug token's label and id, so what a session did to a panel is on record.
            created_by text NOT NULL CHECK (char_length(created_by) <= 200),
            created_at timestamptz NOT NULL DEFAULT now(),
            -- The pickup deadline while queued, the answer deadline once sent.
            expires_at timestamptz NOT NULL,
            picked_up_at timestamptz,
            state text NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'sent', 'done', 'expired')),
            result jsonb CHECK (result IS NULL OR octet_length(result::text) <= 131072),
            png_sha256 text CHECK (png_sha256 IS NULL OR png_sha256 ~ '^[0-9a-f]{64}$'),
            result_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX panel_debug_request_pending"
        " ON app.panel_debug_request (device_id, state, created_at)"
    )

    op.execute("ALTER TABLE app.panel_debug_request ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.panel_debug_request FORCE ROW LEVEL SECURITY")
    # The owner, and the debug console (which runs under an owner context), do everything:
    # queue, read, sweep.
    op.execute(
        """
        CREATE POLICY panel_debug_request_owner ON app.panel_debug_request
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # A panel sees its own live requests and nothing else — not an expired one it never
    # collected, and never a sibling's. The `done` arm exists only because Postgres holds an
    # UPDATE's NEW row to the SELECT policy too (the WHERE reads the row): without it the
    # answer itself is refused. `result_at = now()` is true only inside the transaction that
    # wrote the answer (the trigger stamps it, and `now()` is the transaction's clock), so a
    # finished request is gone from the panel's view the moment its answer commits.
    op.execute(
        f"""
        CREATE POLICY panel_debug_request_panel_read ON app.panel_debug_request
        FOR SELECT USING (
            {_PANEL_OWN}
            AND (
                (state IN ('queued', 'sent') AND expires_at > now())
                OR (state = 'done' AND result_at = now())
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY panel_debug_request_panel_answer ON app.panel_debug_request
        FOR UPDATE
        USING ({_PANEL_OWN} AND state IN ('queued', 'sent') AND expires_at > now())
        WITH CHECK ({_PANEL_OWN} AND state IN ('sent', 'done'))
        """
    )
    # No INSERT or DELETE policy for a panel: with RLS forced, absence is refusal.

    op.execute(
        """
        CREATE FUNCTION app.panel_debug_request_panel_write() RETURNS trigger
        LANGUAGE plpgsql SET search_path = app, pg_temp AS $$
        BEGIN
            IF current_setting('app.principal_kind', true) IS DISTINCT FROM 'device_key' THEN
                RETURN NEW;
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.device_id IS DISTINCT FROM OLD.device_id
               OR NEW.command IS DISTINCT FROM OLD.command
               OR NEW.args IS DISTINCT FROM OLD.args
               OR NEW.confirm IS DISTINCT FROM OLD.confirm
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'a panel may not change the request it was sent';
            END IF;
            IF OLD.state = 'queued' AND NEW.state = 'sent' THEN
                NEW.picked_up_at := now();
                NEW.expires_at := now() + interval '120 seconds';
                NEW.result := NULL;
                NEW.png_sha256 := NULL;
                NEW.result_at := NULL;
            ELSIF OLD.state = 'sent' AND NEW.state = 'done' THEN
                NEW.picked_up_at := OLD.picked_up_at;
                NEW.expires_at := OLD.expires_at;
                NEW.result_at := now();
            ELSE
                RAISE EXCEPTION 'a panel may only collect a queued request or answer a sent one';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER panel_debug_request_panel_write
        BEFORE UPDATE ON app.panel_debug_request
        FOR EACH ROW EXECUTE FUNCTION app.panel_debug_request_panel_write()
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.panel_debug_request TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.panel_debug_request")
    op.execute("DROP FUNCTION app.panel_debug_request_panel_write()")
