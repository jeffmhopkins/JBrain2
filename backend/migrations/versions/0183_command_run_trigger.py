"""A run can be triggered by a radio command, and a key can never be empty.

Two corrections an independent review of P4 found, both of which made the wave fail in
ways no test caught (docs/plans/APRS_CONTROL_PLAN.md P4).

**`trigger`.** `app.task_runs.trigger` has allowed only `schedule` and `manual` since
0093, and the gate fires with `command`. The whole verify path worked — the code was
checked, the counter burned, the attempt recorded `accepted`, the owner's phone told
that the command had run — and then the run insert violated the CHECK, the gate
swallowed it to a log line, and nothing happened. The single worst shape a failure can
take: told it worked, credential spent, nothing done.

**An empty key.** The CHECK required `command_key IS NOT NULL`, and `''` satisfies that.
An empty key is not a refusal — `hmac.new(b"", ...)` is perfectly valid, so the codes
become ones anyone who has read this repository can compute. No route can write one
today; this makes sure none ever can. 32 characters is the base32 of a 16-byte secret,
comfortably under the 32-byte key the box actually generates.
"""

from alembic import op

revision = "0183"
down_revision = "0182"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.task_runs DROP CONSTRAINT task_runs_trigger_check")
    op.execute(
        "ALTER TABLE app.task_runs ADD CONSTRAINT task_runs_trigger_check "
        "CHECK (trigger IN ('schedule', 'manual', 'command'))"
    )
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_command_complete")
    op.execute(
        """
        ALTER TABLE app.tasks ADD CONSTRAINT tasks_command_complete
        CHECK (
            schedule_kind <> 'on_command'
            OR (command_word IS NOT NULL AND length(command_key) >= 32)
        )
        """
    )
    # One command word per station, and a BLANK callsign is the encouraged case — the
    # editor says leaving it blank costs nothing. Postgres treats NULLs as distinct in a
    # unique index unless told otherwise, so the original index let two tasks answer
    # `GATE` from any station. Which one a transmission was judged against then depended
    # on row order: a valid code for the live gate could be judged against a disabled
    # twin, refused, and burn a failure.
    op.execute("DROP INDEX app.tasks_command_word_idx")
    op.execute(
        """
        CREATE UNIQUE INDEX tasks_command_word_idx
        ON app.tasks (principal_id, command_word, command_callsign)
        NULLS NOT DISTINCT
        WHERE schedule_kind = 'on_command'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.tasks_command_word_idx")
    op.execute(
        """
        CREATE UNIQUE INDEX tasks_command_word_idx
        ON app.tasks (principal_id, command_word, command_callsign)
        WHERE schedule_kind = 'on_command'
        """
    )
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT tasks_command_complete")
    op.execute(
        """
        ALTER TABLE app.tasks ADD CONSTRAINT tasks_command_complete
        CHECK (
            schedule_kind <> 'on_command'
            OR (command_word IS NOT NULL AND command_key IS NOT NULL)
        )
        """
    )
    op.execute("DELETE FROM app.task_runs WHERE trigger = 'command'")
    op.execute("ALTER TABLE app.task_runs DROP CONSTRAINT task_runs_trigger_check")
    op.execute(
        "ALTER TABLE app.task_runs ADD CONSTRAINT task_runs_trigger_check "
        "CHECK (trigger IN ('schedule', 'manual'))"
    )
