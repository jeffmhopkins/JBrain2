"""`on_command`: a task fired by a verified radio command (APRS_CONTROL_PLAN.md P4).

The GUI gate chose a fourth TRIGGER KIND over a parallel task system, so this widens
the existing CHECK rather than adding a table: one list, one editor, one runs history.
`Draft`, the row, the runs log and the push are untouched — what is new is the trigger
and the verify path behind it.

An `on_command` task never schedules. Like `on_demand` its `next_run_at` stays NULL and
the scheduler skips it; what fires it is a packet, not the clock.

The credential columns live on the task because the credential IS per-command: one key
and one counter for "open the gate" means burning that command's codes cannot touch
another's, and revoking one is deleting one row.

**`command_key` is a secret.** Owner-only RLS is what protects it, the same as every
other column here — there is no separate vault on this box, and inventing one for a
single value would be a second thing to keep right. It is write-mostly: the owner sets
it up once and the verify path reads it.

`command_failures` is the lockout counter. It is checked BEFORE any comparison, so a
lockout cannot be worn down by continuing to guess, and only the owner clears it.
"""

from alembic import op

revision = "0181"
down_revision = "0180"
branch_labels = None
depends_on = None

_KINDS_OLD = "('on_demand', 'once', 'repeat')"
_KINDS_NEW = "('on_demand', 'once', 'repeat', 'on_command')"


def upgrade() -> None:
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_schedule_kind_check")
    op.execute(
        f"ALTER TABLE app.tasks ADD CONSTRAINT tasks_schedule_kind_check "
        f"CHECK (schedule_kind IN {_KINDS_NEW})"
    )
    op.execute(
        """
        ALTER TABLE app.tasks
            ADD COLUMN command_word text,
            ADD COLUMN command_callsign text,
            ADD COLUMN command_key text,
            ADD COLUMN command_counter bigint NOT NULL DEFAULT 0,
            ADD COLUMN command_failures integer NOT NULL DEFAULT 0,
            ADD COLUMN command_last_at timestamptz,
            -- The ARMING WINDOW: when this command is listening at all. Empty means
            -- always, while the task is enabled. Same shape as a repeat schedule
            -- (days + a local time range in the task's timezone), answering a
            -- different question: not when it RUNS but when it is LISTENING.
            --
            -- Outside its window a command is REFUSED, never queued — which is why
            -- this is evaluated at verify time and is not an ActionSpec.precondition.
            -- A precondition defers, and a deferred gate command is a gate that opens
            -- hours later for someone who is no longer there.
            ADD COLUMN command_days int[] NOT NULL DEFAULT '{}',
            ADD COLUMN command_from text,
            ADD COLUMN command_until text
        """
    )
    # A command task without a word and a key cannot verify anything, and a task that
    # cannot verify must not be able to claim it did. Enforced here rather than only in
    # the API, because the verify path reads this table directly.
    op.execute(
        """
        ALTER TABLE app.tasks ADD CONSTRAINT tasks_command_complete
        CHECK (
            schedule_kind <> 'on_command'
            OR (command_word IS NOT NULL AND command_key IS NOT NULL)
        )
        """
    )
    # One command word per station: two tasks answering "GATE" would make which one
    # fired depend on row order.
    op.execute(
        """
        CREATE UNIQUE INDEX tasks_command_word_idx
        ON app.tasks (principal_id, command_word, command_callsign)
        WHERE schedule_kind = 'on_command'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.tasks_command_word_idx")
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_command_complete")
    op.execute("DELETE FROM app.tasks WHERE schedule_kind = 'on_command'")
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_schedule_kind_check")
    op.execute(
        f"ALTER TABLE app.tasks ADD CONSTRAINT tasks_schedule_kind_check "
        f"CHECK (schedule_kind IN {_KINDS_OLD})"
    )
    op.execute(
        """
        ALTER TABLE app.tasks
            DROP COLUMN command_word,
            DROP COLUMN command_callsign,
            DROP COLUMN command_key,
            DROP COLUMN command_counter,
            DROP COLUMN command_failures,
            DROP COLUMN command_last_at,
            DROP COLUMN command_days,
            DROP COLUMN command_from,
            DROP COLUMN command_until
        """
    )
