"""`endpoint_panel_setting` — the desk panel's settings, as the PWA asks and as the panel reports.

docs/mocks/jpanel-c6/README.md (owner, approved 2026-10-13): the display panel's card in the PWA
gains a "Panel settings" section — the night clock and its hours and colour, the date under the
time, the time zone, the notice switches, brightness, volume, read-aloud and auto-rotate. The
panel stays the source of truth (it keeps its settings in NVS and changes them on the glass);
this row is the queue of what the owner asked for from the PWA, and the last thing the panel
said it holds, so the request survives the panel being off and the box restarting.

- **`desired`** is `{key: {"value": v, "base": b}}`: what the owner set and what the panel
  reported for that key when he set it. The owner's answer to the race (2026-10-13): *the panel's
  last report wins.* A pending change is retried only while the panel still reports `base`; a
  report of `value` settles it; a report of anything else means it was changed at the glass, and
  the change is dropped. `version` moves with every owner change, so the panel can tell a new
  request from the one it already applied.
- **`reported`** is the panel's last report (`{key: value}`), `reported_at` when the box last
  stored it — what the PWA shows when the panel is offline ("as the panel reported them 2 h ago").
- **Per panel, keyed on the subject** like `endpoint_panel_alarm` (0236): a re-flash rotates the
  key and keeps the subject, so a queued change outlives the credentials.

RLS — the alarms pattern, and the column rule a policy cannot give:

- the owner reads and writes everything;
- a panel (`device_key`, pinned to its subject) reads, creates and updates only its own row; a
  sibling's is invisible to it, so the twins' panels never read or write the desk panel's;
- what a panel may write is held by `endpoint_panel_setting_guard`: it may create its row only
  with nothing desired, never move `version` or the row's subject, and may only DROP entries
  from `desired` (a change settled or refused by its report) — never add one or alter one. It
  may write `reported` freely: that is its own word about itself.

Revision ID: 0245
Revises: 0244
Create Date: 2026-10-13
"""

from alembic import op

revision = "0245"
down_revision = "0244"
branch_labels = None
depends_on = None

_OWN_PANEL = """
    current_setting('app.principal_kind', true) = 'device_key'
    AND subject_id::text = current_setting('app.subject_id', true)
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.endpoint_panel_setting (
            subject_id uuid PRIMARY KEY REFERENCES app.subjects(id) ON DELETE CASCADE,
            desired jsonb NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(desired) = 'object'),
            version bigint NOT NULL DEFAULT 0 CHECK (version >= 0),
            reported jsonb CHECK (reported IS NULL OR jsonb_typeof(reported) = 'object'),
            reported_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.endpoint_panel_setting_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            k text;
        BEGIN
            IF current_setting('app.principal_kind', true) = 'device_key' THEN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.desired <> '{}'::jsonb OR NEW.version <> 0 THEN
                        RAISE EXCEPTION 'a panel may not ask for its own settings'
                            USING ERRCODE = 'insufficient_privilege';
                    END IF;
                ELSE
                    IF NEW.subject_id <> OLD.subject_id OR NEW.version <> OLD.version THEN
                        RAISE EXCEPTION 'a panel may only drop a settled or refused change'
                            USING ERRCODE = 'insufficient_privilege';
                    END IF;
                    -- Every entry it keeps must be exactly the owner's: it may drop, never write.
                    FOR k IN SELECT jsonb_object_keys(NEW.desired) LOOP
                        IF NOT (OLD.desired ? k) OR OLD.desired -> k <> NEW.desired -> k THEN
                            RAISE EXCEPTION 'a panel may only drop a settled or refused change'
                                USING ERRCODE = 'insufficient_privilege';
                        END IF;
                    END LOOP;
                END IF;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER endpoint_panel_setting_panel_only_drops
        BEFORE INSERT OR UPDATE ON app.endpoint_panel_setting
        FOR EACH ROW EXECUTE FUNCTION app.endpoint_panel_setting_guard()
        """
    )

    op.execute("ALTER TABLE app.endpoint_panel_setting ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.endpoint_panel_setting FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY endpoint_panel_setting_owner ON app.endpoint_panel_setting
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        f"""
        CREATE POLICY endpoint_panel_setting_own_read ON app.endpoint_panel_setting FOR SELECT
        USING ({_OWN_PANEL})
        """
    )
    op.execute(
        f"""
        CREATE POLICY endpoint_panel_setting_own_insert ON app.endpoint_panel_setting
        FOR INSERT WITH CHECK ({_OWN_PANEL})
        """
    )
    # No DELETE policy for a panel: its queue is the owner's to clear.
    op.execute(
        f"""
        CREATE POLICY endpoint_panel_setting_own_report ON app.endpoint_panel_setting
        FOR UPDATE USING ({_OWN_PANEL}) WITH CHECK ({_OWN_PANEL})
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.endpoint_panel_setting TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.endpoint_panel_setting")
    op.execute("DROP FUNCTION app.endpoint_panel_setting_guard()")
