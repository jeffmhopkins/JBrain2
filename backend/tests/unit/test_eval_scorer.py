"""The live eval `Scorer` (Phase-5 Track H·A): it drives the note.extract suite
through the LLM ADAPTER (a faked router here — never a provider SDK), accumulates
the tokens billed, and returns an `EvalRun` carrying the two-dimensional
`{task, safety}` split the promotion gate depends on.

The model is faked exactly as everywhere else: a `LlmRouter` over a canned client
that returns one usage-bearing response per case. The point under test is the
WIRING (router-driven, tokens summed, suite-filter, version label), not the model's
judgment — the scoring logic itself is proven in `evals/run.py`'s adapters."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from jbrain.llm import LlmRouter
from jbrain.llm.types import LlmImage, LlmResult, LlmUsage, parse_json_payload
from jbrain.queue import PermanentJobError
from jbrain.workflow.eval_scorer import _select_cases, build_live_scorer

# A structurally valid (empty) extraction — every case parses, so the scorer
# exercises the score path rather than the error fallback. Mentions/facts empty
# means most task checks miss, but the run is well-formed with real scores.
_EMPTY_EXTRACTION = json.dumps(
    {"title": "t", "tags": [], "mentions": [], "facts": [], "temporal_tokens": []}
)


class _UsageFake:
    """A canned client that bills a FIXED, non-trivial usage per `complete` call so
    the scorer's token accumulation is observable (FakeLlmClient bills 1+1)."""

    def __init__(self, *, input_tokens: int, output_tokens: int) -> None:
        self._in = input_tokens
        self._out = output_tokens
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        images: Sequence[LlmImage] = (),
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 4096,
        reasoning_effort: str | None = None,
    ) -> LlmResult:
        self.calls += 1
        parsed = parse_json_payload(_EMPTY_EXTRACTION) if json_schema is not None else None
        return LlmResult(text=_EMPTY_EXTRACTION, parsed=parsed, usage=LlmUsage(self._in, self._out))


def _router(client: _UsageFake) -> LlmRouter:
    # note.extract resolves through its capability tier (NOTE_EXTRACT_STRENGTH), whose
    # TIER_DEFAULTS point at the "xai" provider — so the fake is keyed there (the
    # same posture as tests/integration/test_eval_db_runner_pg.py).
    return LlmRouter(
        {"xai": client},  # type: ignore[dict-item]  # only complete() is used
        {"note.extract": ("xai", "grok-4.3")},
    )


async def test_live_scorer_drives_the_adapter_and_sums_tokens() -> None:
    client = _UsageFake(input_tokens=10, output_tokens=5)
    scorer = build_live_scorer(_router(client))

    run, tokens = await scorer("", "cand-v1")

    # One model call per case, all through the router (the LLM adapter).
    n_cases = len(_select_cases(""))
    assert client.calls == n_cases
    # Total billed = sum of (input + output) across every case.
    assert tokens == n_cases * 15
    # The run is labeled with the candidate version and carries one score per case.
    assert run.version == "cand-v1"
    assert len(run.scores) == n_cases
    # The two-dimensional split survived: every fixture has a task AND a safety score
    # (the gate's whole point — a flat blob would defeat it).
    for s in run.scores:
        assert 0.0 <= s.task <= 1.0
        assert 0.0 <= s.safety <= 1.0


async def test_live_scorer_suite_filter_selects_a_slice() -> None:
    client = _UsageFake(input_tokens=1, output_tokens=1)
    scorer = build_live_scorer(_router(client))

    # A name-substring suite scores only the matching cases (a subset of the whole).
    full = len(_select_cases("all"))
    temporal_run, _tokens = await scorer("temporal", "cand")
    assert 0 < len(temporal_run.scores) < full
    assert client.calls == len(temporal_run.scores)


def test_select_cases_all_vs_filter() -> None:
    assert _select_cases("") == _select_cases("all")
    assert len(_select_cases("temporal")) < len(_select_cases(""))


def test_select_cases_fails_closed_when_filter_matches_nothing() -> None:
    # A suite that selects zero cases is a fail-closed PermanentJobError, not an
    # empty run: scoring nothing would silently store a contentless EvalRun.
    with pytest.raises(PermanentJobError):
        _select_cases("no-such-case-name-zzz")
