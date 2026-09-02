"""The verify path against real Postgres (APRS_CONTROL_PLAN.md P4).

This wave's exit criteria are security claims, and every one of them is about SQL that
has to behave under concurrency and constraint: "a command from the truck fires an
allowlisted action; a replay does nothing; a forged callsign does nothing; every attempt
is visible." So the tests run against a real database rather than a fake — the atomic
consume IS a conditional UPDATE, and a fake would happily let the wrong one through.

The radio and the agent are the parts that are faked: a `Heard` stands in for the
antenna, and a recording stub stands in for the task runner. What is exercised for real
is everything between them.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.notify import NotifyBus
from jbrain.sdr.command import MAX_FAILURES, code_for, key_to_text, new_key
from jbrain.sdr.gate import CommandGate, Heard
from jbrain.tasks.repo import TaskInfo, TaskRepo, TaskRunRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

KEY = new_key()
WHEN = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)  # a Wednesday, midday UTC


@dataclass
class _Runner:
    """Records what would have been run, and writes the run row FOR REAL.

    The agent turn is faked; the row is not. That split matters more than it looks: the
    original fake stopped at recording the call, and so the whole suite passed while a
    verified command could not fire at all — `trigger="command"` violated a CHECK on
    `app.task_runs` that had allowed only schedule and manual since the table was made.
    Everything upstream worked, the counter burned, the owner's phone said it had run,
    and the insert raised into a swallowed log line.

    A fake that stops short of the database cannot catch that, and a Postgres suite whose
    docstring claims it does not use fakes really must not use one HERE."""

    fired: list[tuple[str, str]]
    runs: TaskRunRepo | None = None

    async def run(self, owner_ctx: SessionContext, task: TaskInfo, *, trigger: str) -> Any:
        run_id = None
        if self.runs is not None:
            run_id = await self.runs.start(
                owner_ctx,
                task_id=task.id,
                principal_id=owner_ctx.principal_id,
                session_id=None,
                run_id=None,
                trigger=trigger,
            )
        self.fired.append((task.id, trigger))
        return SimpleNamespace(id=run_id) if run_id else None


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    """One database serves the module, and a command word is UNIQUE per station — so
    every test starts from an empty tasks table rather than colliding with the last."""
    yield
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.command_attempts"))
        await s.execute(text("DELETE FROM app.task_runs"))
        await s.execute(text("DELETE FROM app.tasks"))
        await s.commit()


async def _owner(maker: async_sessionmaker) -> str:
    pid = OWNER.principal_id
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash)"
                " VALUES (:pid, 'owner', :hash) ON CONFLICT (id) DO NOTHING"
            ),
            {"pid": pid, "hash": f"h-{pid}"},
        )
        await s.commit()
    return str(pid)


async def _command(maker: async_sessionmaker, **overrides: Any) -> str:
    """An `on_command` task, defaulted to the simplest possible one."""
    pid = await _owner(maker)
    fields: dict[str, Any] = {
        "pid": pid,
        "word": "GATE",
        "callsign": None,
        "key": key_to_text(KEY),
        "counter": 0,
        "failures": 0,
        "days": [],
        "from": None,
        "until": None,
        "once": False,
        "enabled": True,
        "tz": "UTC",
        **overrides,
    }
    async with scoped_session(maker, OWNER) as s:
        task_id = (
            await s.execute(
                text(
                    "INSERT INTO app.tasks (principal_id, name, prompt, agent, schedule_kind,"
                    " enabled, timezone, command_word, command_callsign, command_key,"
                    " command_counter, command_failures, command_days, command_from,"
                    " command_until, command_once)"
                    " VALUES (:pid, 'Gate', 'open the gate', 'jerv', 'on_command', :enabled,"
                    " :tz, :word, :callsign, :key, :counter, :failures, :days, :from,"
                    " :until, :once)"
                    " RETURNING id"
                ),
                fields,
            )
        ).scalar()
        await s.commit()
    return str(task_id)


def _gate(maker: async_sessionmaker) -> tuple[CommandGate, _Runner]:
    runner = _Runner(fired=[], runs=TaskRunRepo(maker))
    gate = CommandGate(
        maker,
        repo=TaskRepo(maker),
        runner=runner,
        notify=NotifyBus(),
    )
    return gate, runner


async def _attempts(maker: async_sessionmaker) -> list[dict[str, Any]]:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT source, word, code, accepted, reason, task_id"
                    " FROM app.command_attempts ORDER BY heard_at"
                )
            )
        ).mappings()
        return [dict(r) for r in rows]


async def _counters(maker: async_sessionmaker, task_id: str) -> tuple[int, int]:
    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text("SELECT command_counter, command_failures FROM app.tasks WHERE id = :id"),
                {"id": task_id},
            )
        ).mappings()
        got = row.one()
        return int(got["command_counter"]), int(got["command_failures"])


def _heard(info: str, source: str = "KE8XYZ-9", when: datetime = WHEN) -> Heard:
    return Heard(source=source, info=info, heard_at=when)


async def test_a_command_from_the_truck_fires_the_task(maker: async_sessionmaker) -> None:
    task_id = await _command(maker)
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    assert attempt is not None and attempt.accepted
    assert runner.fired == [(task_id, "command")]


async def test_a_replay_does_nothing(maker: async_sessionmaker) -> None:
    task_id = await _command(maker)
    gate, runner = _gate(maker)
    frame = _heard(f"GATE {code_for(KEY, 0)}")

    first = await gate.offer(frame)
    second = await gate.offer(frame)

    # Everyone in range heard that code. Hearing it again is not authorisation.
    assert first is not None and first.accepted
    assert second is not None and not second.accepted
    assert runner.fired == [(task_id, "command")]
    assert (await _counters(maker, task_id))[0] == 1


async def test_a_forged_callsign_does_nothing(maker: async_sessionmaker) -> None:
    await _command(maker, callsign="KE8XYZ")
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}", source="N0BODY-1"))

    # The code was RIGHT — this is the filter, and it is only a filter, doing its job
    # against someone who does not have the key anyway.
    assert attempt is not None and not attempt.accepted
    assert runner.fired == []


async def test_the_ssid_is_not_part_of_the_callsign_filter(maker: async_sessionmaker) -> None:
    task_id = await _command(maker, callsign="KE8XYZ")
    gate, runner = _gate(maker)

    # The truck is -9 and the HT is -7; an owner who typed the bare call meant both.
    assert (await gate.offer(_heard(f"GATE {code_for(KEY, 0)}", source="KE8XYZ-7"))).accepted
    assert runner.fired == [(task_id, "command")]


async def test_an_ssid_the_owner_typed_is_honoured_exactly(maker: async_sessionmaker) -> None:
    await _command(maker, callsign="KE8XYZ-9")
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}", source="KE8XYZ-7"))

    assert not attempt.accepted
    assert runner.fired == []


async def test_a_wrong_code_counts_toward_the_lockout(maker: async_sessionmaker) -> None:
    task_id = await _command(maker)
    gate, runner = _gate(maker)

    await gate.offer(_heard("GATE AAAAA"))

    assert runner.fired == []
    assert (await _counters(maker, task_id))[1] == 1


async def test_the_lockout_stops_accepting_even_the_right_code(maker: async_sessionmaker) -> None:
    task_id = await _command(maker, failures=MAX_FAILURES)
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    # Deliberate: past the lockout the box stops deciding and waits for the owner. A
    # lockout that a valid code could clear would be worn down by whoever caused it.
    assert not attempt.accepted
    assert "locked out" in attempt.reason
    assert runner.fired == []
    assert (await _counters(maker, task_id))[0] == 0


async def test_a_good_code_clears_the_failures_it_follows(maker: async_sessionmaker) -> None:
    task_id = await _command(maker, failures=MAX_FAILURES - 1)
    gate, _ = _gate(maker)

    await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    # One mis-keyed digit on the way out must not leave the owner one guess from lockout.
    assert await _counters(maker, task_id) == (1, 0)


async def test_a_disabled_command_is_off_not_merely_quiet(maker: async_sessionmaker) -> None:
    task_id = await _command(maker, enabled=False)
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    assert not attempt.accepted
    assert runner.fired == []
    # The counter is untouched: the switch is the outermost gate, before the credential.
    assert (await _counters(maker, task_id))[0] == 0


async def test_a_command_outside_its_window_is_refused_not_queued(
    maker: async_sessionmaker,
) -> None:
    task_id = await _command(maker, **{"from": "07:00", "until": "09:00"})
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}", when=WHEN))

    assert not attempt.accepted and "window" in attempt.reason
    assert runner.fired == []
    # Not counted as a failure either: an out-of-hours transmission must not spend the
    # owner's lockout budget on their behalf.
    assert await _counters(maker, task_id) == (0, 0)


async def test_inside_its_window_the_same_command_works(maker: async_sessionmaker) -> None:
    task_id = await _command(maker, **{"from": "07:00", "until": "13:00"})
    gate, runner = _gate(maker)

    assert (await gate.offer(_heard(f"GATE {code_for(KEY, 0)}", when=WHEN))).accepted
    assert runner.fired == [(task_id, "command")]


async def test_ordinary_channel_traffic_is_not_an_attempt(maker: async_sessionmaker) -> None:
    await _command(maker)
    gate, runner = _gate(maker)

    for chatter in ("=4903.50N/07201.75W-Op Jeff", "hello there", "GATE"):
        assert await gate.offer(_heard(chatter)) is None

    # A packet frequency is mostly other people. Recording those would bury the rows
    # that matter, and "hello there" parses as two words — the WORD is what saves it.
    assert await _attempts(maker) == []
    assert runner.fired == []


async def test_every_attempt_is_visible_afterwards(maker: async_sessionmaker) -> None:
    await _command(maker)
    gate, _ = _gate(maker)

    await gate.offer(_heard("GATE AAAAA"))
    await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    rows = await _attempts(maker)
    # The refusal is the row worth having: three of these from an unknown station last
    # Tuesday is a fact the owner has to be able to find, and a push does not keep.
    assert [(r["accepted"], r["code"], r["source"]) for r in rows] == [
        (False, "AAAAA", "KE8XYZ-9"),
        (True, code_for(KEY, 0), "KE8XYZ-9"),
    ]
    assert all(r["word"] == "GATE" for r in rows)


async def test_a_sender_ahead_of_the_box_resyncs_rather_than_wedging(
    maker: async_sessionmaker,
) -> None:
    task_id = await _command(maker)
    gate, _ = _gate(maker)

    # Three transmissions that never decoded: the truck is at 3, the box still at 0.
    assert (await gate.offer(_heard(f"GATE {code_for(KEY, 3)}"))).accepted
    assert (await _counters(maker, task_id))[0] == 4


async def test_a_code_from_behind_the_counter_is_dead(maker: async_sessionmaker) -> None:
    task_id = await _command(maker, counter=5)
    gate, runner = _gate(maker)

    assert not (await gate.offer(_heard(f"GATE {code_for(KEY, 4)}"))).accepted
    assert runner.fired == []
    assert (await _counters(maker, task_id))[0] == 5


async def test_a_command_word_with_no_task_is_silent(maker: async_sessionmaker) -> None:
    await _command(maker)
    gate, runner = _gate(maker)

    assert await gate.offer(_heard(f"OPEN {code_for(KEY, 0)}")) is None
    assert runner.fired == []


async def test_an_unreadable_key_refuses_rather_than_crashing(maker: async_sessionmaker) -> None:
    await _command(maker, key="!" * 40)
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    # A bad key is the owner's mistake to fix, not a reason for the drain loop to die.
    assert not attempt.accepted
    assert runner.fired == []


# --- what the first review of this wave found -----------------------------------------


async def _runs(maker: async_sessionmaker) -> list[str]:
    async with scoped_session(maker, OWNER) as s:
        rows = (await s.execute(text("SELECT trigger FROM app.task_runs"))).all()
        return [r.trigger for r in rows]


async def test_a_verified_command_actually_WRITES_a_run(maker: async_sessionmaker) -> None:
    """The whole point of the wave, and it did not work.

    `app.task_runs.trigger` allowed only schedule and manual, so the run insert violated
    a CHECK, the gate swallowed it, and the gate never opened — after telling the owner
    it had. Everything else in this file passed throughout, because the runner was faked
    all the way past the database."""
    await _command(maker)
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    assert attempt.accepted
    assert runner.fired  # the call happened
    assert await _runs(maker) == ["command"]  # and the row survived the constraint


async def test_a_digipeated_command_does_not_lock_the_owner_out(
    maker: async_sessionmaker,
) -> None:
    """144.390 is digipeated: one transmission arrives several times.

    Every copy after the first fails to verify — the counter has moved past it — so
    scoring them as guesses spends the whole lockout budget on the owner SUCCEEDING, and
    refuses their next genuine command. The lockout is supposed to fire on attack."""
    task_id = await _command(maker)
    gate, runner = _gate(maker)
    frame = _heard(f"GATE {code_for(KEY, 0)}")

    for _ in range(6):
        await gate.offer(frame)

    assert runner.fired == [(task_id, "command")]  # fired exactly once
    counter, failures = await _counters(maker, task_id)
    assert (counter, failures) == (1, 0)  # and burned NO lockout budget
    # The next genuine command still works, which is the property that actually matters.
    assert (await gate.offer(_heard(f"GATE {code_for(KEY, 1)}"))).accepted


async def test_a_repeat_is_still_refused_even_though_it_is_forgiven(
    maker: async_sessionmaker,
) -> None:
    # Forgiving the lockout is not accepting the code. Forward-only is what makes it safe
    # that everyone in range heard it.
    task_id = await _command(maker)
    gate, runner = _gate(maker)
    frame = _heard(f"GATE {code_for(KEY, 0)}")
    await gate.offer(frame)

    second = await gate.offer(frame)

    assert not second.accepted and second.reason == "code already used"
    assert runner.fired == [(task_id, "command")]


async def test_a_wrong_code_STILL_counts_after_all_that(maker: async_sessionmaker) -> None:
    # The forgiveness must not have quietly disabled the lockout: a code that was never
    # ours is a guess, and five of them stop the command.
    task_id = await _command(maker)
    gate, _ = _gate(maker)

    for _ in range(5):
        await gate.offer(_heard("GATE AAAAA"))

    assert (await _counters(maker, task_id))[1] == 5
    assert "locked out" in (await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))).reason


async def test_one_hostile_byte_cannot_erase_the_evidence(maker: async_sessionmaker) -> None:
    """Postgres `text` cannot hold a NUL, `_record` swallows its own errors, and the code
    comparison strips control characters before matching. So a NUL-suffixed code behaved
    exactly like a clean one while leaving no row at all — five of them locked the command
    with nothing recorded anywhere. One byte against the table the design leans on."""
    await _command(maker)
    gate, _ = _gate(maker)

    await gate.offer(_heard("GATE AAAAA\x00"))

    rows = await _attempts(maker)
    assert len(rows) == 1
    assert rows[0]["accepted"] is False
    assert "\x00" not in rows[0]["code"]


async def test_two_copies_arriving_at_once_fire_once(maker: async_sessionmaker) -> None:
    """The atomic consume, which nothing tested.

    Dropping `AND command_counter = :seen` from the UPDATE passed every test in this
    repository — and it is the mechanism the plan names as what makes it safe that the
    command travels in clear. Sequential duplicates take the spent-code path, so this
    branch is only reachable under genuine concurrency: two receivers, or a digipeat
    landing while the first copy is still being judged."""
    task_id = await _command(maker)
    gate, runner = _gate(maker)
    frame = _heard(f"GATE {code_for(KEY, 0)}")

    await asyncio.gather(*(gate.offer(frame) for _ in range(4)))

    assert runner.fired == [(task_id, "command")]
    assert (await _counters(maker, task_id))[0] == 1
    assert await _runs(maker) == ["command"]


async def test_the_database_refuses_an_empty_key_outright(maker: async_sessionmaker) -> None:
    """`hmac.new(b"", ...)` does not raise — it produces codes anyone who has read this
    repository can compute, so an empty key is not a broken credential but a public one.

    The old CHECK only asked for NOT NULL, which `''` satisfies. No route could write one,
    but "one column value away from total compromise" is the wrong distance. Now the
    constraint makes the state unreachable, and `verify` refuses a short key besides
    (tests/unit/test_sdr_command.py) — the two layers are deliberate."""
    task_id = await _command(maker)

    with pytest.raises((IntegrityError, DBAPIError)):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text("UPDATE app.tasks SET command_key = '' WHERE id = :id"), {"id": task_id}
            )
            await s.commit()


async def test_two_commands_cannot_share_a_word_when_neither_names_a_station(
    maker: async_sessionmaker,
) -> None:
    """A blank callsign is the ENCOURAGED case — the editor says leaving it blank costs
    nothing — and Postgres treats NULLs as distinct in a unique index unless told
    otherwise. So the index meant to stop two tasks answering GATE did not, and which one
    a transmission was judged against depended on row order: a valid code for the live
    gate could be judged against a disabled twin, refused, and burn a failure."""
    await _command(maker)

    with pytest.raises(IntegrityError):
        await _command(maker)


async def test_a_one_shot_command_disarms_itself(maker: async_sessionmaker) -> None:
    """The mock's third arming mode: hand out one code, it works once, then the command
    is off until the owner arms it again (`b-trigger-editor.html`, shape A)."""
    task_id = await _command(maker, once=True)
    gate, runner = _gate(maker)

    assert (await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))).accepted

    async with scoped_session(maker, OWNER) as s:
        enabled = (
            await s.execute(text("SELECT enabled FROM app.tasks WHERE id = :id"), {"id": task_id})
        ).scalar()
    assert enabled is False
    # And it stays off: the next code finds a disabled command, which is the outermost
    # gate and refuses before the credential is even consulted.
    assert not (await gate.offer(_heard(f"GATE {code_for(KEY, 1)}"))).accepted
    assert runner.fired == [(task_id, "command")]


async def test_a_one_shot_command_fires_once_even_under_a_digipeat_race(
    maker: async_sessionmaker,
) -> None:
    """Why the disarm is in the same statement as the consume.

    Turning the task off afterwards, as a second write, leaves a window in which a
    duplicate of that same transmission — the normal case on a digipeated channel —
    finds a command that is still armed."""
    task_id = await _command(maker, once=True)
    gate, runner = _gate(maker)
    frame = _heard(f"GATE {code_for(KEY, 0)}")

    await asyncio.gather(*(gate.offer(frame) for _ in range(4)))

    assert runner.fired == [(task_id, "command")]
    assert await _runs(maker) == ["command"]


async def test_an_accepted_attempt_points_at_the_run_it_started(
    maker: async_sessionmaker,
) -> None:
    # The join the owner would actually want: "a command was accepted at 06:14" is only
    # half a fact without "and here is what it did".
    await _command(maker)
    gate, _ = _gate(maker)

    await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    async with scoped_session(maker, OWNER) as s:
        linked = (
            await s.execute(
                text(
                    "SELECT a.run_id, r.id FROM app.command_attempts a"
                    " JOIN app.task_runs r ON r.id = a.run_id"
                )
            )
        ).all()
    assert len(linked) == 1
