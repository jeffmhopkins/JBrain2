"""The integrate.note eval scorer (jbrain.evals.integrate_runner): judgment
scoring (resolve / supersede / accumulate / conflict / cross_subject / ambiguous),
the data-integrity safety guards (never mint a name, no sentence in value_json),
and a mini-audit of the committed corpus. The model is faked — CI never calls a box."""

import json
from typing import Any

from jbrain.evals.integrate_runner import (
    build_inputs,
    eval_run_from_integrate,
    load_integrate_cases,
    score_integrate_cases,
)
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter

INTEGRATE_TASK = "integrate.note"


def _router(responses: list[str]) -> LlmRouter:
    return LlmRouter(
        {"fake": FakeLlmClient(responses=responses)},
        {INTEGRATE_TASK: ("fake", "m")},
        tiers={"high": ("fake", "m"), "low": ("fake", "m")},
    )


def _intent(resolutions: list[dict], facts: list[dict], supersessions: list[dict]) -> str:
    return json.dumps(
        {
            "resolutions": resolutions,
            "facts": facts,
            "supersession_proposals": supersessions,
            "merge_proposals": [],
            "distinct_proposals": [],
        }
    )


_OWNER = {"id": "ent-owner", "name": "Jeff Hopkins", "kind": "Person", "facts": []}


def _case(name: str, gold: dict, **kw: Any) -> dict:
    base = {
        "name": name,
        "note_text": "n",
        "mentions": [{"name": "Me", "kind": "Person", "surface": "I"}],
        "facts": [
            {
                "entity_ref": "Me",
                "predicate": "worksFor",
                "qualifier": "",
                "kind": "relationship",
                "statement": "s",
                "value_json": None,
                "assertion": "asserted",
                "object_entity_ref": "Atlas",
            }
        ],
        "owner": _OWNER,
        "others": [],
        "gold": gold,
    }
    base.update(kw)
    return base


async def test_supersede_and_value_guard_pass_on_a_correct_intent() -> None:
    case = _case("supersede", {"supersede": {"worksFor": "supersede"}})
    intent = _intent(
        [
            {"mention_ref": "Me", "mode": "existing", "entity_id": "ent-owner"},
            {
                "mention_ref": "Atlas",
                "mode": "new",
                "new_kind": "Organization",
                "new_name": "Atlas",
            },
        ],
        [
            {
                "entity_ref": "Me",
                "predicate": "worksFor",
                "kind": "relationship",
                "assertion": "asserted",
                "statement": "I work for Atlas.",
                "object_entity_ref": "Atlas",
            }
        ],
        [{"entity_ref": "Me", "predicate": "worksFor", "qualifier": "", "action": "supersede"}],
    )
    results, tokens = await score_integrate_cases(_router([intent]), [case])
    assert results[0].passed
    assert tokens == 2  # FakeLlmClient bills 1+1


async def test_minting_a_name_trips_the_safety_guard() -> None:
    case = _case(
        "nickname",
        {"resolve_existing": {"Me": "ent-owner"}, "no_mint_name": ["Cel"]},
        facts=[
            {
                "entity_ref": "Me",
                "predicate": "name.nickname",
                "qualifier": "",
                "kind": "attribute",
                "statement": "goes by Cel",
                "value_json": {"value": "Cel"},
                "assertion": "asserted",
            }
        ],
    )
    intent = _intent(
        [
            {"mention_ref": "Me", "mode": "existing", "entity_id": "ent-owner"},
            {"mention_ref": "Cel", "mode": "new", "new_kind": "Person", "new_name": "Cel"},
        ],
        [
            {
                "entity_ref": "Me",
                "predicate": "name.nickname",
                "kind": "attribute",
                "assertion": "asserted",
                "statement": "goes by Cel",
                "value_json": {"value": "Cel"},
            }
        ],
        [],
    )
    results, _ = await score_integrate_cases(_router([intent]), [case])
    r = results[0]
    assert not r.passed
    assert any(label.startswith("no_mint_name:") and not ok for label, ok, _ in r.checks)


async def test_sentence_in_value_json_trips_the_safety_guard() -> None:
    case = _case("prose_value", {})
    intent = _intent(
        [{"mention_ref": "Me", "mode": "existing", "entity_id": "ent-owner"}],
        [
            {
                "entity_ref": "Me",
                "predicate": "note",
                "kind": "state",
                "assertion": "asserted",
                "statement": "x",
                "value_json": {
                    "value": "He was admitted to the hospital on Tuesday "
                    "after the fall and stayed three nights for observation."
                },
            }
        ],
        [],
    )
    results, _ = await score_integrate_cases(_router([intent]), [case])
    r = results[0]
    assert any(label.startswith("no_value_sentence:") and not ok for label, ok, _ in r.checks)
    assert not r.passed


async def test_eval_run_splits_task_and_safety() -> None:
    good = _case("good", {"supersede": {"worksFor": "supersede"}})
    good_intent = _intent(
        [{"mention_ref": "Me", "mode": "existing", "entity_id": "ent-owner"}],
        [],
        [{"entity_ref": "Me", "predicate": "worksFor", "qualifier": "", "action": "supersede"}],
    )
    results, _ = await score_integrate_cases(_router([good_intent]), [good])
    run = eval_run_from_integrate(results, "integrate-test")
    s = run.scores[0]
    assert s.task == 1.0 and s.safety == 1.0


def test_build_inputs_is_pure_and_renders_owner_context() -> None:
    cases = load_integrate_cases()
    extraction, ctx = build_inputs(cases[0])
    assert extraction.facts and "Owner/author:" in ctx


def test_committed_corpus_is_well_formed() -> None:
    cases = load_integrate_cases()
    assert len(cases) >= 8
    seen: set[str] = set()
    for c in cases:
        assert c["name"] not in seen, f"duplicate case name {c['name']}"
        seen.add(c["name"])
        assert c["mentions"] and c["facts"] and c["owner"] and isinstance(c["gold"], dict)
        # Every entity_ref a fact uses must be a declared mention (or the owner "Me").
        names = {m["name"] for m in c["mentions"]}
        for f in c["facts"]:
            assert f["entity_ref"] in names or f["entity_ref"] == "Me"
