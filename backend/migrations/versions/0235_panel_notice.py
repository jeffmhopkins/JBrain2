"""`panel_notice` — the box taps the owner on the shoulder through his desk panel.

docs/plans/JPANEL_C6_PLAN.md §2 (wave C3b). Jerv, Ops or a workflow puts a message on the
panel — text, optionally a picture and a short sound — and may ask a question whose answer
goes back to the sender. One row per notice, short-lived: queued -> delivered -> shown ->
answered / dismissed / expired, swept a while after it finishes.

**A refused notice is never a row.** The box turns a send away at the door (the panel is
offline, switched notices off, or is inside its night window) from what the panel last
reported, so nothing here waits to pop up later. Everything in this table was accepted.

**The panel is a writer here, so the policies say what it may write** — the same shape as
`panel_debug_request` (0231), for the same reason: the key lives in flash on a desk. A panel
reads only its OWN live notices, may only move them along, and may record only which of the
offered choices was tapped. It can create none, delete none, and never see a sibling's.

RLS chooses rows, not columns, so a trigger holds the rest for a `device_key` caller: what
the notice says (title, body, choices, image, audio, sender) is immutable; the only moves are
queued -> delivered -> shown -> answered / dismissed / expired; an answer must be one of the
notice's own choices, byte for byte; and the timestamps are the box's — delivery, not the
panel, starts the answer window, so a panel cannot keep a question answerable forever.

`image_sha256` (the picture as the panel draws it, already encoded) and `audio_sha256` (16 kHz
s16 PCM) are blob digests, registered in `jbrain.blob_refs` in the same change.

Revision ID: 0235
Revises: 0234
Create Date: 2026-10-12
"""

from alembic import op

revision = "0235"
down_revision = "0234"
branch_labels = None
depends_on = None

_PANEL_OWN = """
    current_setting('app.principal_kind', true) = 'device_key'
    AND device_id::text = current_setting('app.principal_id', true)
"""

_SHA = "~ '^[0-9a-f]{64}$'"


def upgrade() -> None:
    # Choices are a short list of short labels: the panel draws at most four buttons, each a
    # word or two. Checked here as well as at the API so no path can store a fifth.
    op.execute(
        """
        CREATE FUNCTION app.panel_notice_choices_ok(c jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$
            SELECT jsonb_typeof(c) = 'array'
               AND jsonb_array_length(c) <= 4
               AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(c) e
                   WHERE jsonb_typeof(e) <> 'string'
                      OR octet_length(e #>> '{}') NOT BETWEEN 1 AND 16
               )
        $$
        """
    )
    op.execute(
        f"""
        CREATE TABLE app.panel_notice (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            device_id uuid NOT NULL REFERENCES app.principals(id) ON DELETE CASCADE,
            sender text NOT NULL
                CHECK (sender ~ '^(jerv|ops|owner|debug|workflow:[a-z0-9][a-z0-9_.-]{{0,47}})$'),
            title text NOT NULL CHECK (octet_length(title) BETWEEN 1 AND 60),
            body text NOT NULL DEFAULT '' CHECK (octet_length(body) <= 300),
            choices jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (app.panel_notice_choices_ok(choices)),
            chime boolean NOT NULL DEFAULT false,
            -- Seconds the notice stays up once shown; NULL: until it is tapped.
            hide_after_s integer CHECK (hide_after_s IS NULL OR hide_after_s BETWEEN 3 AND 600),
            -- How long an answer is accepted once the panel has it.
            ttl_s integer NOT NULL CHECK (ttl_s BETWEEN 30 AND 86400),
            image_mode text CHECK (image_mode IN ('full', 'banner')),
            image_w smallint CHECK (image_w IS NULL OR image_w BETWEEN 1 AND 480),
            image_h smallint CHECK (image_h IS NULL OR image_h BETWEEN 1 AND 480),
            image_sha256 text CHECK (image_sha256 IS NULL OR image_sha256 {_SHA}),
            image_bytes integer CHECK (image_bytes IS NULL OR image_bytes > 0),
            audio_sha256 text CHECK (audio_sha256 IS NULL OR audio_sha256 {_SHA}),
            audio_ms integer CHECK (audio_ms IS NULL OR audio_ms BETWEEN 1 AND 8000),
            CHECK ((image_sha256 IS NULL) = (image_mode IS NULL)
                   AND (image_sha256 IS NULL) = (image_w IS NULL)
                   AND (image_sha256 IS NULL) = (image_h IS NULL)
                   AND (image_sha256 IS NULL) = (image_bytes IS NULL)),
            CHECK ((audio_sha256 IS NULL) = (audio_ms IS NULL)),
            -- Who sent it, for the record: the token's label, "jerv (session …)", a workflow.
            created_by text NOT NULL CHECK (char_length(created_by) <= 200),
            created_at timestamptz NOT NULL DEFAULT now(),
            -- The pickup deadline while queued; the answer deadline once delivered.
            expires_at timestamptz NOT NULL,
            state text NOT NULL DEFAULT 'queued' CHECK (
                state IN ('queued', 'delivered', 'shown', 'answered', 'dismissed', 'expired')
            ),
            answer text CHECK (answer IS NULL OR octet_length(answer) BETWEEN 1 AND 16),
            CHECK ((state = 'answered') = (answer IS NOT NULL)),
            delivered_at timestamptz,
            shown_at timestamptz,
            finished_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX panel_notice_pending ON app.panel_notice (device_id, state, created_at)"
    )

    op.execute("ALTER TABLE app.panel_notice ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.panel_notice FORCE ROW LEVEL SECURITY")
    # The owner, and every box-side sender (Jerv's turn, Ops, a workflow, the debug console —
    # all of which run under an owner context), do everything: send, read, sweep.
    op.execute(
        """
        CREATE POLICY panel_notice_owner ON app.panel_notice
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # A panel sees its own live notices and nothing else. The finished arm exists only because
    # Postgres holds an UPDATE's NEW row to the SELECT policy too: `finished_at = now()` is true
    # only inside the transaction that finished it (the trigger stamps it), so a notice leaves
    # the panel's view the moment its answer commits — as 0231's `done` arm does.
    op.execute(
        f"""
        CREATE POLICY panel_notice_panel_read ON app.panel_notice
        FOR SELECT USING (
            {_PANEL_OWN}
            AND (
                (state IN ('queued', 'delivered', 'shown') AND expires_at > now())
                OR (state IN ('answered', 'dismissed', 'expired') AND finished_at = now())
            )
        )
        """
    )
    op.execute(
        f"""
        CREATE POLICY panel_notice_panel_state ON app.panel_notice
        FOR UPDATE
        USING ({_PANEL_OWN} AND state IN ('queued', 'delivered', 'shown') AND expires_at > now())
        WITH CHECK (
            {_PANEL_OWN}
            AND state IN ('delivered', 'shown', 'answered', 'dismissed', 'expired')
        )
        """
    )
    # No INSERT or DELETE policy for a panel: with RLS forced, absence is refusal.

    op.execute(
        """
        CREATE FUNCTION app.panel_notice_panel_write() RETURNS trigger
        LANGUAGE plpgsql SET search_path = app, pg_temp AS $$
        BEGIN
            IF current_setting('app.principal_kind', true) IS DISTINCT FROM 'device_key' THEN
                RETURN NEW;
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.device_id IS DISTINCT FROM OLD.device_id
               OR NEW.sender IS DISTINCT FROM OLD.sender
               OR NEW.title IS DISTINCT FROM OLD.title
               OR NEW.body IS DISTINCT FROM OLD.body
               OR NEW.choices IS DISTINCT FROM OLD.choices
               OR NEW.chime IS DISTINCT FROM OLD.chime
               OR NEW.hide_after_s IS DISTINCT FROM OLD.hide_after_s
               OR NEW.ttl_s IS DISTINCT FROM OLD.ttl_s
               OR NEW.image_mode IS DISTINCT FROM OLD.image_mode
               OR NEW.image_w IS DISTINCT FROM OLD.image_w
               OR NEW.image_h IS DISTINCT FROM OLD.image_h
               OR NEW.image_sha256 IS DISTINCT FROM OLD.image_sha256
               OR NEW.image_bytes IS DISTINCT FROM OLD.image_bytes
               OR NEW.audio_sha256 IS DISTINCT FROM OLD.audio_sha256
               OR NEW.audio_ms IS DISTINCT FROM OLD.audio_ms
               OR NEW.created_by IS DISTINCT FROM OLD.created_by
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'a panel may not change the notice it was sent';
            END IF;
            -- Whatever the caller wrote, the clock columns are the box's.
            NEW.expires_at := OLD.expires_at;
            NEW.delivered_at := OLD.delivered_at;
            NEW.shown_at := OLD.shown_at;
            NEW.finished_at := NULL;
            IF OLD.state = 'queued' AND NEW.state = 'delivered' THEN
                NEW.delivered_at := now();
                NEW.expires_at := now() + make_interval(secs => OLD.ttl_s);
                NEW.answer := NULL;
            ELSIF OLD.state = 'delivered' AND NEW.state = 'shown' THEN
                NEW.shown_at := now();
                NEW.answer := NULL;
            ELSIF OLD.state IN ('delivered', 'shown') AND NEW.state = 'answered' THEN
                IF NEW.answer IS NULL OR NOT (OLD.choices ? NEW.answer) THEN
                    RAISE EXCEPTION 'an answer must be one of the notice''s choices';
                END IF;
                NEW.shown_at := coalesce(OLD.shown_at, now());
                NEW.finished_at := now();
            ELSIF OLD.state IN ('delivered', 'shown') AND NEW.state IN ('dismissed', 'expired') THEN
                NEW.answer := NULL;
                NEW.finished_at := now();
            ELSE
                RAISE EXCEPTION 'a panel may only move a notice queued -> delivered -> shown -> '
                                'answered / dismissed / expired';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER panel_notice_panel_write
        BEFORE UPDATE ON app.panel_notice
        FOR EACH ROW EXECUTE FUNCTION app.panel_notice_panel_write()
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.panel_notice TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.panel_notice")
    op.execute("DROP FUNCTION app.panel_notice_panel_write()")
    op.execute("DROP FUNCTION app.panel_notice_choices_ok(jsonb)")
