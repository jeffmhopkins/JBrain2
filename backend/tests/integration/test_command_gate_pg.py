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

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.notify import NotifyBus
from jbrain.sdr.command import MAX_FAILURES, code_for, key_to_text, new_key
from jbrain.sdr.gate import CommandGate, Heard
from jbrain.tasks.repo import TaskInfo, TaskRepo
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
    """Records what would have been run. The gate's job ends at "fire this task"."""

    fired: list[tuple[str, str]]

    async def run(self, owner_ctx: SessionContext, task: TaskInfo, *, trigger: str) -> Any:
        self.fired.append((task.id, trigger))
        return None


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
                    " command_until)"
                    " VALUES (:pid, 'Gate', 'open the gate', 'jerv', 'on_command', :enabled,"
                    " :tz, :word, :callsign, :key, :counter, :failures, :days, :from, :until)"
                    " RETURNING id"
                ),
                fields,
            )
        ).scalar()
        await s.commit()
    return str(task_id)


def _gate(maker: async_sessionmaker) -> tuple[CommandGate, _Runner]:
    runner = _Runner(fired=[])
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
    await _command(maker, key="not base32!!")
    gate, runner = _gate(maker)

    attempt = await gate.offer(_heard(f"GATE {code_for(KEY, 0)}"))

    # A bad key is the owner's mistake to fix, not a reason for the drain loop to die.
    assert not attempt.accepted
    assert runner.fired == []
