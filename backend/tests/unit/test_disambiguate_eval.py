"""The entity.disambiguate eval scorer (jbrain.evals.disambiguate_runner): the
link-decision scoring and the {task, safety} split, plus a mini-audit that the
committed corpus is well-formed. The model is faked — CI never calls a box."""

from typing import Any

from jbrain.evals.disambiguate_runner import (
    eval_run_from_disambiguate,
    load_disambiguate_cases,
    score_disambiguate_cases,
)
from jbrain.llm.router import LlmRouter

DISAMBIGUATE_TASK = "entity.disambiguate"


def _router(responses: list[str]) -> LlmRouter:
    from jbrain.llm.fake import FakeLlmClient

    client = FakeLlmClient(responses=responses)
    # The runner calls with strength="low"; map the tier (and the task) to the fake.
    return LlmRouter(
        {"fake": client},
        {DISAMBIGUATE_TASK: ("fake", "m")},
        tiers={"low": ("fake", "m"), "high": ("fake", "m")},
    )


def _choice(name: str, entity_id: str | None) -> str:
    import json

    return json.dumps({"choices": [{"name": name, "entity_id": entity_id}]})


_CASES: list[dict[str, Any]] = [
    {"name": "c_link", "mention": "Bob", "kind": "Person", "context": "ctx",
     "candidates": [{"id": "e1", "name": "Bob Reyes", "kind": "Person", "summary": "s"}],
     "gold": "e1"},
    {"name": "c_null", "mention": "Sam", "kind": "Person", "context": "ctx",
     "candidates": [{"id": "e2", "name": "Sam W", "kind": "Person", "summary": "s"},
                    {"id": "e3", "name": "Sam O", "kind": "Person", "summary": "s"}],
     "gold": None},
    {"name": "c_false", "mention": "X", "kind": "Person", "context": "ctx",
     "candidates": [{"id": "e4", "name": "X Y", "kind": "Person", "summary": "s"}],
     "gold": None},
    {"name": "c_missed", "mention": "Y", "kind": "Person", "context": "ctx",
     "candidates": [{"id": "e5", "name": "Y Z", "kind": "Person", "summary": "s"}],
     "gold": "e5"},
]


async def test_scores_link_null_false_and_missed() -> None:
    responses = [
        _choice("Bob", "e1"),  # correct link
        _choice("Sam", None),  # correct "none of these"
        _choice("X", "e4"),  # FALSE link — gold was null
        _choice("Y", None),  # missed — gold was e5
    ]
    results, tokens = await score_disambiguate_cases(_router(responses), _CASES)
    by = {r.name: r for r in results}
    assert by["c_link"].passed and by["c_null"].passed
    assert not by["c_false"].passed  # both the link AND the false-link guard fail
    assert not by["c_missed"].passed  # the link check fails (conservative miss)
    assert tokens == len(_CASES) * 2  # FakeLlmClient bills 1+1 per call


async def test_eval_run_splits_task_and_safety() -> None:
    responses = [_choice("Bob", "e1"), _choice("Sam", None), _choice("X", "e4"), _choice("Y", None)]
    results, _ = await score_disambiguate_cases(_router(responses), _CASES)
    run = eval_run_from_disambiguate(results, "entity-disambiguate-test")
    scores = {s.fixture: s for s in run.scores}
    # A false link tanks BOTH dimensions; a conservative miss tanks only task.
    assert (scores["c_false"].task, scores["c_false"].safety) == (0.0, 0.0)
    assert (scores["c_missed"].task, scores["c_missed"].safety) == (0.0, 1.0)
    assert (scores["c_link"].task, scores["c_link"].safety) == (1.0, 1.0)
    assert (scores["c_null"].task, scores["c_null"].safety) == (1.0, 1.0)


async def test_unanswered_mention_on_a_link_case_is_a_miss() -> None:
    # Empty choices on a gold=id case: not linking is the wrong call (a miss), but
    # never a false link (no entity was fused).
    results, _ = await score_disambiguate_cases(_router(['{"choices": []}']), _CASES[:1])
    r = results[0]
    assert not r.passed and r.got is None
    assert all(ok for label, ok, _ in r.checks if label.startswith("no_false_link:"))


async def test_omitting_a_null_gold_mention_is_correct_not_a_miss() -> None:
    # gold is "none of these": omitting the mention (empty choices) is the SAME
    # correct decision as an explicit null — not linking — so it must score task=1,
    # not be penalized. (Half the corpus is null-gold; this is the common path.)
    null_case = [_CASES[1]]  # gold is None
    results, _ = await score_disambiguate_cases(_router(['{"choices": []}']), null_case)
    assert results[0].passed and results[0].got is None


def test_committed_corpus_is_well_formed() -> None:
    cases = load_disambiguate_cases()
    assert len(cases) >= 8
    seen: set[str] = set()
    for c in cases:
        assert c["name"] not in seen, f"duplicate case name {c['name']}"
        seen.add(c["name"])
        assert c["mention"] and isinstance(c["candidates"], list) and c["candidates"]
        assert "gold" in c, f"{c['name']} missing gold"
        ids = {cand["id"] for cand in c["candidates"]}
        assert c["gold"] is None or c["gold"] in ids, f"{c['name']} gold not among candidate ids"
