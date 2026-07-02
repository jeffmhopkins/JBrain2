"""The eval DB-mode runner end to end against real Postgres, LLM faked.

Proves run_case_db's WIRING — seed rows → extract → integrate → plan_intent →
apply_intent → COMMIT → read the committed graph back into a DbCommit — and that
check_case_db then passes on that real commit. The gate LOGIC itself is proven
purely in tests/unit/test_eval_assertions_db.py; here we prove the plumbing that
feeds it real rows (dispositions round-trip, seeded entities resolve, prior
facts read back). The real-Grok run (tests/eval/run.py --db) is the opt-in gate.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import text

from jbrain.analysis.predicates import record_predicate_alias
from jbrain.db.session import scoped_session
from jbrain.llm import LlmRouter
from jbrain.llm.types import LlmImage, LlmResult, LlmUsage, parse_json_payload
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.eval.assertions import check_case_db
from tests.eval.cases import case_from_dict
from tests.eval.runner import run_case_db
from tests.integration.test_extraction_pg import maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class _ScriptedFake:
    """Like FakeLlmClient but each response is a callable of the user_text, so a
    test can inject a seeded entity's UUID (read from graph_context) into the
    integrate response — the resolve-to-existing path a static script can't fake."""

    def __init__(self, scripts: Sequence[Any]) -> None:
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

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
        i = len(self.calls)
        self.calls.append({"user_text": user_text})
        text = self._scripts[min(i, len(self._scripts) - 1)](user_text)
        parsed = parse_json_payload(text) if json_schema is not None else None
        return LlmResult(text=text, parsed=parsed, usage=LlmUsage(1, 1))


def _router(scripts: Sequence[Any]) -> LlmRouter:
    return LlmRouter(
        {"xai": _ScriptedFake(scripts)},  # type: ignore[dict-item]  # only complete() is used
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )


def _fixed(text: str):
    return lambda _user_text: text


async def test_run_case_db_dispositions_round_trip(maker, tmp_path):  # noqa: F811
    # One note mints two entities: a surface-attested fact commits ACTIVE; a
    # cross-subject fact is HELD pending_review + a card. Both must read back into
    # the DbCommit and pass check_case_db.
    extract = json.dumps(
        {
            "title": "Work",
            "tags": ["work"],
            "mentions": [
                {"name": "Globex", "kind": "Organization", "surface_text": "Globex"},
                {"name": "Initech", "kind": "Organization", "surface_text": "Initech"},
            ],
            "facts": [],
            "temporal_tokens": [],
        }
    )
    intent = json.dumps(
        {
            "resolutions": [
                {
                    "mention_ref": "Globex",
                    "mode": "new",
                    "new_kind": "Organization",
                    "new_name": "Globex",
                },
                {
                    "mention_ref": "Initech",
                    "mode": "new",
                    "new_kind": "Organization",
                    "new_name": "Initech",
                    "cross_subject": True,
                },
            ],
            "facts": [
                {
                    "entity_ref": "Globex",
                    "predicate": "industry",
                    "kind": "attribute",
                    "assertion": "asserted",
                    "statement": "Globex is in tech",
                    "self_confidence": 0.95,
                    "surface": "Globex",
                },
                {
                    "entity_ref": "Initech",
                    "predicate": "industry",
                    "kind": "attribute",
                    "assertion": "asserted",
                    "statement": "Initech is in tech",
                    "self_confidence": 0.95,
                    "surface": "Initech",
                },
            ],
        }
    )
    case = case_from_dict(
        {
            "id": "wire-dispositions",
            "note_text": "Globex is in tech. Initech is in tech.",
            "expect": {
                "facts": [
                    {"entity": "Globex", "predicate": "industry", "disposition": "commit"},
                    {"entity": "Initech", "predicate": "industry", "disposition": "review"},
                ]
            },
        }
    )

    commit = await run_case_db(
        _router([_fixed(extract), _fixed(intent)]), case, maker=maker, tmp_path=tmp_path
    )

    assert {f.entity_name for f in commit.facts} == {"Globex", "Initech"}
    globex = next(f for f in commit.facts if f.entity_name == "Globex")
    initech = next(f for f in commit.facts if f.entity_name == "Initech")
    assert globex.status == "active" and globex.id not in commit.review_fact_ids
    assert initech.status == "pending_review" and initech.id in commit.review_fact_ids
    assert check_case_db(case, commit) == []


async def test_run_case_db_resolves_to_seeded_entity(maker, tmp_path):  # noqa: F811
    # A seeded entity (with a prior fact) must resolve from the note's mention
    # onto the SAME row — no forked duplicate — and the prior fact must read back.
    def integrate(user_text: str) -> str:
        # The seeded entity's real UUID appears on its graph_context line (which
        # renders "id '<uuid>' name 'Globex'"); the owner line names "Me".
        ent_id = next(
            (
                m.group()
                for line in user_text.splitlines()
                if "Globex" in line
                for m in [_UUID_RE.search(line)]
                if m
            ),
            None,
        )
        return json.dumps(
            {
                "resolutions": [{"mention_ref": "Globex", "mode": "existing", "entity_id": ent_id}],
                "facts": [],
            }
        )

    extract = json.dumps(
        {
            "title": "Work",
            "tags": ["work"],
            "mentions": [{"name": "Globex", "kind": "Organization", "surface_text": "Globex"}],
            "facts": [],
            "temporal_tokens": [],
        }
    )
    case = case_from_dict(
        {
            "id": "wire-resolve",
            "note_text": "Globex shipped a new product.",
            "seed": [
                {
                    "id": "ent-globex",
                    "name": "Globex",
                    "kind": "Organization",
                    "facts": [{"predicate": "industry", "kind": "attribute", "value": "tech"}],
                }
            ],
            "expect": {
                "resolutions": [
                    {"mention": "Globex", "mode": "existing", "entity_id": "ent-globex"}
                ]
            },
        }
    )

    commit = await run_case_db(
        _router([_fixed(extract), integrate]), case, maker=maker, tmp_path=tmp_path
    )

    # The mention resolved onto the seeded UUID; no second Globex row was minted.
    assert "ent-globex" in commit.seeded_ids
    new_globex = [
        n
        for eid, n in commit.entities.items()
        if n == "Globex" and eid != commit.seeded_ids["ent-globex"]
    ]
    assert new_globex == []
    # The seeded prior fact read back (its supersession state is observable).
    assert any(s.predicate == "industry" for s in commit.seeded_facts)
    assert check_case_db(case, commit) == []


_HIRE_STMT = "Pat was hired by Globex."


async def test_run_case_db_canonicalizes_a_drift_predicate(maker, tmp_path):  # noqa: F811
    # Proves the --canon harness wiring: an unknown predicate with a durable
    # predicate_aliases entry (a past owner map_to_existing decision) is
    # rewritten before the arbiter keys the fact — no embedder involved
    # (the two-tier model: the alias collapse is all canonicalize does now).
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind)"
                " VALUES ('worksFor', 'd', 'ref', 'relationship')"
            )
        )
        await record_predicate_alias(session, "isHiredBy", "worksFor")

    extract = json.dumps(
        {
            "title": "Work",
            "tags": [],
            "mentions": [
                {"name": "Pat", "kind": "Person", "surface_text": "Pat"},
                {"name": "Globex", "kind": "Organization", "surface_text": "Globex"},
            ],
            "facts": [],
            "temporal_tokens": [],
        }
    )
    intent = json.dumps(
        {
            "resolutions": [
                {"mention_ref": "m1", "mode": "new", "new_kind": "Person", "new_name": "Pat"},
                {
                    "mention_ref": "m2",
                    "mode": "new",
                    "new_kind": "Organization",
                    "new_name": "Globex",
                },
            ],
            "facts": [
                {
                    "entity_ref": "m1",
                    "predicate": "isHiredBy",
                    "kind": "relationship",
                    "assertion": "asserted",
                    "statement": _HIRE_STMT,
                    "object_entity_ref": "m2",
                    "self_confidence": 0.95,
                    "surface": "hired",
                }
            ],
        }
    )
    case = case_from_dict({"id": "canon-wire", "note_text": _HIRE_STMT})

    commit = await run_case_db(
        _router([_fixed(extract), _fixed(intent)]),
        case,
        maker=maker,
        tmp_path=tmp_path,
        canonicalize=True,
    )

    preds = {f.predicate for f in commit.facts}
    assert "worksFor" in preds  # the drift predicate was rewritten to the canonical
    assert "ishiredby" not in {p.lower() for p in preds}
    assert commit.review_cards == ()  # the raw spelling filed no new_predicate card
