"""`endpoint_panel_alarm` — the owner's desk-panel alarms, set in the PWA, switched on the glass.

docs/mocks/jpanel-c6/README.md, owner decision: alarms are created and edited in the PWA
(jpanel → Panels → Jeff's); on the panel they can only be switched on and off. The panel keeps
its own copy and rings from its own clock, so this table is the source it syncs from, not
something it asks at ring time.

- **Per panel, keyed on the subject** like `endpoint_panel` (0212): a re-flash rotates the key
  but keeps the subject, so the alarms outlive the credentials.
- **`minute`** is the time of day in the PANEL's zone (the panel computes daylight saving
  itself; the box never converts). **`days`** is the firmware's own mask: bit 0 Monday … bit 6
  Sunday, 0 = once — the panel switches a one-off off after it rings.
- **At most eight** per panel (`ALARM_MAX` on the panel), held by a trigger that takes a
  per-subject advisory lock before it counts, so concurrent inserts cannot both pass; the
  route's own count is only the friendly early answer.

RLS — three answers, and the third is the one a policy alone cannot give:

- the owner reads and writes everything;
- a panel (`device_key`, pinned to its subject) reads only its own rows, and a sibling's are
  invisible to it;
- a panel may UPDATE its own rows (the switch on the glass) but only the `enabled` flag:
  `endpoint_panel_alarm_panel_only_switches` refuses any other column changing under a
  `device_key` session. A policy decides WHICH rows; it cannot decide which columns, and a
  column GRANT cannot tell the owner from a panel because both connect as `jbrain_app`.

Revision ID: 0236
Revises: 0235
Create Date: 2026-10-12
"""

from alembic import op

revision = "0236"
down_revision = "0235"
branch_labels = None
depends_on = None

#: The panel's own row test, shared by the read and the switch policies.
_OWN_PANEL = """
    current_setting('app.principal_kind', true) = 'device_key'
    AND subject_id::text = current_setting('app.subject_id', true)
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.endpoint_panel_alarm (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            subject_id uuid NOT NULL REFERENCES app.subjects(id) ON DELETE CASCADE,
            minute smallint NOT NULL CHECK (minute BETWEEN 0 AND 1439),
            days smallint NOT NULL DEFAULT 0 CHECK (days BETWEEN 0 AND 127),
            enabled boolean NOT NULL DEFAULT true,
            -- What the panel can draw: printable ASCII, 24 at most (the route says so first),
            -- and never a double quote or a backslash: JSON escapes those, and the panel's
            -- fetch buffer is sized for eight full labels with nothing escaped.
            label text NOT NULL DEFAULT ''
                CHECK (octet_length(label) <= 24 AND label ~ '^[ -~]*$'
                       AND position(chr(34) IN label) = 0 AND position(chr(92) IN label) = 0),
            sound text NOT NULL DEFAULT 'chime' CHECK (sound IN ('chime', 'beeps', 'rise')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX endpoint_panel_alarm_subject ON app.endpoint_panel_alarm (subject_id)")

    op.execute(
        """
        CREATE FUNCTION app.endpoint_panel_alarm_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                -- Serialize this panel's inserts before counting: without it two concurrent
                -- inserts each count seven and both land. Keyed on the subject, so other
                -- panels never wait; released at commit.
                PERFORM pg_advisory_xact_lock(
                    hashtextextended('endpoint_panel_alarm:' || NEW.subject_id::text, 0));
                IF (SELECT count(*) FROM app.endpoint_panel_alarm
                    WHERE subject_id = NEW.subject_id) >= 8 THEN
                    RAISE EXCEPTION 'a panel holds at most 8 alarms'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF current_setting('app.principal_kind', true) = 'device_key'
               AND (NEW.subject_id, NEW.minute, NEW.days, NEW.label, NEW.sound, NEW.created_at)
                   IS DISTINCT FROM
                   (OLD.subject_id, OLD.minute, OLD.days, OLD.label, OLD.sound, OLD.created_at)
            THEN
                RAISE EXCEPTION 'a panel may only switch its alarms on or off'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER endpoint_panel_alarm_panel_only_switches
        BEFORE INSERT OR UPDATE ON app.endpoint_panel_alarm
        FOR EACH ROW EXECUTE FUNCTION app.endpoint_panel_alarm_guard()
        """
    )

    op.execute("ALTER TABLE app.endpoint_panel_alarm ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.endpoint_panel_alarm FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY endpoint_panel_alarm_owner ON app.endpoint_panel_alarm
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        f"""
        CREATE POLICY endpoint_panel_alarm_own_read ON app.endpoint_panel_alarm FOR SELECT
        USING ({_OWN_PANEL})
        """
    )
    # FOR UPDATE only: no INSERT or DELETE policy for a panel, so a panel can neither create an
    # alarm nor remove one. WITH CHECK keeps the row its own after the write.
    op.execute(
        f"""
        CREATE POLICY endpoint_panel_alarm_own_switch ON app.endpoint_panel_alarm FOR UPDATE
        USING ({_OWN_PANEL}) WITH CHECK ({_OWN_PANEL})
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.endpoint_panel_alarm TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.endpoint_panel_alarm")
    op.execute("DROP FUNCTION app.endpoint_panel_alarm_guard()")
