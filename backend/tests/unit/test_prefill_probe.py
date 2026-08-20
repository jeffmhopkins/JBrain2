"""The prefill diagnostic: what it captures, when it fires, and what it must never leak."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

import structlog

from jbrain.llm import prefill_probe

# Every case drives the schedule directly, so nothing here waits the real three seconds.
FAST = (0.02, 0.02, 0.02)


class _Reader:
    """Stands in for `LocalGatewayClient.slots`, recording what it was asked for."""

    def __init__(self, body: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self._body = body if body is not None else [{"id": 0}]
        self._fail = fail
        self.calls: list[str] = []

    async def __call__(self, served_model: str) -> list[dict[str, object]]:
        self.calls.append(served_model)
        if self._fail:
            raise RuntimeError("model is not resident")
        return [dict(s) for s in self._body]


def _captures(logs: Sequence[Any]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "llm.prefill_slots"]


def _slots(entry: dict[str, Any]) -> list[dict[str, Any]]:
    return json.loads(entry["slots"])


# --- redaction ------------------------------------------------------------------------
# The probe reads an endpoint that carries the in-flight PROMPT, and writes into a log the
# debug API serves back. Since the schema is unknown, these pin the property that has to hold
# for ANY schema: keys and numbers survive, content does not.


def test_every_string_is_replaced_by_its_length() -> None:
    shaped = prefill_probe._shape({"prompt": "my blood pressure was 150/95 this morning"})
    assert shaped == {"prompt": "<str:41>"}


def test_strings_nested_anywhere_are_redacted_too() -> None:
    # The field that leaks will not be called `prompt`, because if it were, a deny-list would
    # have been enough. Depth and naming must not matter.
    shaped = prefill_probe._shape(
        {"next_token": {"text_to_send": "secret", "cache": [{"content": "also secret"}]}}
    )
    assert shaped == {"next_token": {"text_to_send": "<str:6>", "cache": [{"content": "<str:11>"}]}}


def test_a_long_array_is_reported_as_a_count() -> None:
    # A prompt travels as token ids as well as text, and that array is REVERSIBLE — the same
    # leak wearing integers. It is also what would make one log line enormous.
    shaped = prefill_probe._shape({"prompt_token_ids": list(range(4096))})
    assert shaped == {"prompt_token_ids": "<list:4096>"}


def test_numbers_and_booleans_survive_intact() -> None:
    # The whole point of the capture: whichever of these turns out to be the prefill counter
    # has to arrive readable, and a fraction needs its denominator as well as its numerator.
    shaped = prefill_probe._shape(
        {"id": 0, "is_processing": True, "n_prompt_tokens": 8192, "progress": 0.5, "task": None}
    )
    assert shaped == {
        "id": 0,
        "is_processing": True,
        "n_prompt_tokens": 8192,
        "progress": 0.5,
        "task": None,
    }


def test_a_short_array_of_numbers_is_kept() -> None:
    # Small numeric arrays are shape worth having (a per-slot vector, a version triple) and
    # cannot hold a prompt at this length.
    assert prefill_probe._shape({"n": [1, 2, 3]}) == {"n": [1, 2, 3]}


# --- when it fires --------------------------------------------------------------------


async def test_a_turn_that_answers_quickly_is_never_probed() -> None:
    reader = _Reader()
    async with prefill_probe.watch(reader, "gpt-oss-120b", schedule=(5.0,)) as streaming:
        streaming()
    assert reader.calls == [], "a fast turn must not cost a gateway round trip"


async def test_a_slow_turn_is_sampled_repeatedly() -> None:
    # One reading cannot say which field is the progress; a series can, because only the
    # prefill counters move between frames.
    reader = _Reader()
    with structlog.testing.capture_logs() as logs:
        async with prefill_probe.watch(reader, "gpt-oss-120b", schedule=FAST):
            await asyncio.sleep(0.15)
    assert reader.calls == ["gpt-oss-120b"] * 3
    assert [e["sample"] for e in _captures(logs)] == [0, 1, 2]


async def test_sampling_stops_the_moment_the_first_token_arrives() -> None:
    reader = _Reader()
    async with prefill_probe.watch(reader, "gpt-oss-120b", schedule=(0.02, 0.2, 0.2)) as streaming:
        await asyncio.sleep(0.1)  # past the first capture, nowhere near the second
        streaming()
        await asyncio.sleep(0.5)  # ...and past both of the rest, had they still been due
    assert len(reader.calls) == 1


async def test_the_series_is_bounded() -> None:
    # A minute-long prefill does not teach more than its first few seconds, and this line is
    # written on a box whose logs an operator has to read back through the debug API.
    reader = _Reader()
    with structlog.testing.capture_logs() as logs:
        async with prefill_probe.watch(reader, "gpt-oss-120b", schedule=FAST):
            await asyncio.sleep(0.3)
    assert len(_captures(logs)) == 3


async def test_a_probe_that_cannot_read_says_so_and_does_not_raise() -> None:
    # An instrument that reports nothing is indistinguishable from a box that never went slow
    # — the exact failure that let a dead load percentage ship three times.
    with structlog.testing.capture_logs() as logs:
        async with prefill_probe.watch(_Reader(fail=True), "gpt-oss-120b", schedule=(0.02,)):
            await asyncio.sleep(0.1)
    assert [e["event"] for e in logs] == ["llm.prefill_slots_failed"]


async def test_no_reader_starts_no_task() -> None:
    before = len(asyncio.all_tasks())
    async with prefill_probe.watch(None, "gpt-oss-120b", schedule=FAST) as streaming:
        assert len(asyncio.all_tasks()) == before, "a cloud box must pay nothing for this"
        streaming()


async def test_abandoning_the_turn_leaves_no_probe_running() -> None:
    reader = _Reader()
    async with prefill_probe.watch(reader, "gpt-oss-120b", schedule=FAST):
        pass
    await asyncio.sleep(0.1)
    assert reader.calls == [], "the watch must not outlive the turn it was watching"


async def test_the_capture_records_the_body_and_the_slot_count() -> None:
    reader = _Reader([{"id": 0, "is_processing": True, "prompt": "hello"}])
    with structlog.testing.capture_logs() as logs:
        async with prefill_probe.watch(reader, "gpt-oss-120b", schedule=(0.02,)):
            await asyncio.sleep(0.1)
    (entry,) = _captures(logs)
    assert entry["model"] == "gpt-oss-120b"
    assert entry["n_slots"] == 1
    assert _slots(entry) == [{"id": 0, "is_processing": True, "prompt": "<str:5>"}]


async def test_only_the_first_few_slots_are_written_but_all_are_counted() -> None:
    reader = _Reader([{"id": n} for n in range(9)])
    with structlog.testing.capture_logs() as logs:
        async with prefill_probe.watch(reader, "gpt-oss-120b", schedule=(0.02,)):
            await asyncio.sleep(0.1)
    (entry,) = _captures(logs)
    assert entry["n_slots"] == 9
    assert [s["id"] for s in _slots(entry)] == [0, 1, 2, 3]
