"""Skill distillation pure/seam logic (no DB): the single-domain fail-closed classifier and
`_distill_one` — distill → shadow skill + owner `skill-promotion` proposal, with the reusable and
dedup drops. Candidate selection + the gate live in the integration test (they need Postgres)."""

import json
from typing import Any

from jbrain.agent.skilldistill import SkillDistillAction, _Candidate, _domain_of
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter


def test_domain_is_most_sensitive_scope_fail_closed() -> None:
    assert _domain_of(("general", "health")) == "health"  # most-sensitive wins
    assert _domain_of(("general", "finance")) == "finance"
    assert _domain_of(()) == "general"  # no scope → general (the run touched nothing sensitive)


class _FakeSkills:
    def __init__(self, near: float | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self._near = near

    async def nearest_distance(self, ctx: Any, domain_code: str, qvec: Any) -> float | None:
        return self._near

    async def create(self, ctx: Any, **kw: Any) -> str:
        self.created.append(kw)
        return "skill-1"


class _FakeProposals:
    def __init__(self) -> None:
        self.staged: list[Any] = []

    async def stage(self, ctx: Any, *, principal_id: str, spec: Any) -> str:
        self.staged.append(spec)
        return "prop-1"


class _FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


def _router(payload: dict[str, Any]) -> LlmRouter:
    fake = FakeLlmClient(responses=[json.dumps(payload)])
    return LlmRouter({"xai": fake}, {}, tiers={"high": ("xai", "m")})


def _action(skills: _FakeSkills, proposals: _FakeProposals, payload: dict[str, Any]) -> Any:
    return SkillDistillAction(
        None,  # type: ignore[arg-type]  # maker unused by _distill_one
        router=_router(payload),
        embedder=_FakeEmbed(),  # type: ignore[arg-type]
        embedding_model="fake",
        settings=None,  # type: ignore[arg-type]  # gate unused by _distill_one
        skills=skills,  # type: ignore[arg-type]
        proposals=proposals,  # type: ignore[arg-type]
    )


_CAND = _Candidate(
    run_id="r1",
    started_at="2026-06-17T00:00:00+00:00",
    tool_names=("search", "read_note"),
    prose="I searched and cited the note.",
    session_scopes=("general", "health"),
)


async def test_distill_one_writes_shadow_and_stages_owner_proposal() -> None:
    skills, proposals = _FakeSkills(), _FakeProposals()
    action = _action(
        skills,
        proposals,
        {
            "name": "Cite a fact",
            "description": "d",
            "body": "1. search 2. read_note",
            "reusable": True,
        },
    )
    await action._distill_one(_CAND, "owner-pid")
    assert len(skills.created) == 1
    created = skills.created[0]
    assert (
        created["status"] == "shadow" and created["domain_code"] == "health"
    )  # fail-closed domain
    assert len(proposals.staged) == 1
    spec = proposals.staged[0]
    assert spec.kind == "skill-promotion"
    assert spec.nodes[0].op == "skill_promote"
    assert spec.nodes[0].preview["skill_id"] == "skill-1"  # links the proposal to the shadow skill


async def test_distill_one_drops_non_reusable() -> None:
    skills, proposals = _FakeSkills(), _FakeProposals()
    action = _action(
        skills, proposals, {"name": "", "description": "", "body": "", "reusable": False}
    )
    await action._distill_one(_CAND, "owner-pid")
    assert skills.created == [] and proposals.staged == []  # nothing written, nothing staged


async def test_distill_one_drops_near_duplicate() -> None:
    skills, proposals = _FakeSkills(near=0.01), _FakeProposals()  # under the dedup distance
    action = _action(
        skills,
        proposals,
        {"name": "Dup", "description": "d", "body": "1. a 2. b", "reusable": True},
    )
    await action._distill_one(_CAND, "owner-pid")
    assert skills.created == [] and proposals.staged == []  # a near-duplicate is skipped
