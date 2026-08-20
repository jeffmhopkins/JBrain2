"""The prefill fraction, against slot bodies captured verbatim from the live box.

The bodies in `_BUSY` / `_SETTLED` are real — read off llama-server on 2026-08-20 while a
12,286-token prompt was being eaten by gpt-oss-120b. That is the point of them: this module
exists because three previous attempts at a progress figure were written against a GUESSED
shape and shipped dead, so its tests are written against the shape the box actually sends.
"""

from __future__ import annotations

import asyncio

import pytest

from jbrain.llm import prefill

# Captured mid-prefill. Note `n_prompt_tokens` (6290) is NOT the total: the turn's real total
# was 12,286, and this field reads `processed + batch + cache` while the slot is busy.
_BUSY: dict[str, object] = {
    "id": 0,
    "n_ctx": 131072,
    "speculative": False,
    "is_processing": True,
    "id_task": 215,
    "n_prompt_tokens": 6290,
    "n_prompt_tokens_processed": 4096,
    "n_prompt_tokens_cache": 146,
    "next_token": [
        {"has_next_token": False, "has_new_line": False, "n_remain": 40, "n_decoded": 0}
    ],
}

# The same slot once the turn finished: processed back to zero, and `n_prompt_tokens` now
# holding the true total.
_SETTLED: dict[str, object] = {
    "id": 0,
    "is_processing": False,
    "id_task": 215,
    "n_prompt_tokens": 12286,
    "n_prompt_tokens_processed": 0,
    "n_prompt_tokens_cache": 0,
    "next_token": [
        {"has_next_token": False, "has_new_line": False, "n_remain": 40, "n_decoded": 0}
    ],
}


def test_the_fraction_comes_off_the_processed_counter() -> None:
    # 4096 of an estimated 12286 — the numerator is the only exact number in the body.
    assert prefill._fraction([_BUSY], 12286) == pytest.approx(4096 / 12286)


def test_a_settled_slot_reports_nothing() -> None:
    # `is_processing` false is the whole guard: without it the settled body above reads as
    # 0% and the bar would snap back to empty the instant the answer arrived.
    assert prefill._fraction([_SETTLED], 12286) is None


def test_a_generating_slot_reports_nothing() -> None:
    # Prefill is over the moment tokens start coming. The slot stays `is_processing`, so
    # `n_decoded` is what separates "still eating the prompt" from "answering".
    generating = {**_BUSY, "next_token": [{"n_decoded": 3, "n_remain": 37}]}
    assert prefill._fraction([generating], 12286) is None


def test_an_unrecognised_body_reports_nothing_rather_than_guessing() -> None:
    # A llama.cpp rebuild that renames the counter must go quiet, not invent a number. This
    # box floats on a digest off master, so the rename is a when, not an if.
    assert prefill._fraction([{"is_processing": True, "prompt_progress": 0.5}], 12286) is None


def test_the_fraction_is_clamped_to_arrival() -> None:
    # The denominator is an ESTIMATE, so a prompt that tokenizes denser than predicted would
    # otherwise report over 100%. Reads as arrived instead.
    assert prefill._fraction([_BUSY], 1000) == 1.0


def test_no_estimate_means_no_bar() -> None:
    assert prefill._fraction([_BUSY], 0) is None


def test_calibration_moves_the_estimate_toward_the_measured_ratio() -> None:
    model = "test-calibration-model"
    before = prefill.estimate_tokens(model, 10_000)
    # A turn whose 10k characters really were 5k tokens: 2.0 chars/token, far denser than the
    # 3.7 default, so the next estimate for the same characters must come out higher.
    prefill.calibrate(model, 10_000, 5_000)
    after = prefill.estimate_tokens(model, 10_000)
    assert after > before
    # Damped, not snapped: one turn moves the ratio partway, so a single odd prompt (a wall
    # of JSON, a tool transcript) cannot take the estimate hostage.
    assert after < 5_000


def test_calibration_ignores_a_turn_that_reported_no_usage() -> None:
    # Local servers may omit usage; dividing by that zero would poison the ratio for the
    # life of the process.
    model = "test-zero-usage-model"
    before = prefill.estimate_tokens(model, 1_000)
    prefill.calibrate(model, 1_000, 0)
    assert prefill.estimate_tokens(model, 1_000) == before


async def test_a_turn_that_answers_quickly_is_never_read() -> None:
    # The cache-hit case, measured on the box: an identical repeat prompt returns instantly.
    # Reading `/slots` for it would cost a round trip to describe a wait that did not happen.
    reads: list[str] = []

    async def slots(model: str) -> list[dict[str, object]]:
        reads.append(model)
        return [_BUSY]

    async with prefill.watch(
        slots, "m", prompt_chars=40_000, on_progress=_collect([]), first_delay=5.0
    ) as answered:
        answered()
    assert reads == []


async def test_a_slow_turn_publishes_the_fraction_until_it_answers() -> None:
    published: list[float] = []

    async def slots(_model: str) -> list[dict[str, object]]:
        return [_BUSY]

    async with prefill.watch(
        slots,
        "m",
        prompt_chars=int(12286 * 3.7),  # ≈ the real token count at the default ratio
        on_progress=_collect(published),
        first_delay=0.01,
    ) as answered:
        await asyncio.sleep(0.05)
        answered()
    assert published, "a turn silent past the first delay must publish something"
    assert all(0.0 <= value <= 1.0 for value in published)
    assert published[0] == pytest.approx(4096 / 12286, rel=0.02)


async def test_a_read_that_fails_stops_watching_rather_than_failing_the_turn() -> None:
    async def boom(_model: str) -> list[dict[str, object]]:
        raise RuntimeError("gateway down")

    published: list[float] = []
    async with prefill.watch(
        boom, "m", prompt_chars=1_000, on_progress=_collect(published), first_delay=0.01
    ) as answered:
        await asyncio.sleep(0.05)
        answered()
    assert published == []


async def test_no_reader_starts_no_task() -> None:
    # A cloud route, or local hosting off. `watch` must be free there, not merely harmless.
    published: list[float] = []
    async with prefill.watch(
        None, "m", prompt_chars=1_000, on_progress=_collect(published)
    ) as answered:
        answered()
    assert published == []


def _collect(sink: list[float]):  # noqa: ANN202 - a two-line test helper
    async def publish(value: float) -> None:
        sink.append(value)

    return publish
