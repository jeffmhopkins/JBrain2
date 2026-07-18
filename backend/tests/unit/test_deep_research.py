"""The deep_research state machine (docs/proposed/DEEP_RESEARCH_TOOL_PLAN.md): the
plan → gather → reflect → refill → synthesize → critique/revise sequence, the
narrow-only complexity skip matrix, the fixed two-round bound, budget charging, and
the refusal paths — all proven with fakes (a scripted router + a fake fan), no DB, no
real model. The security-critical fan clamps live in test_spawn.py; here we assert the
ORCHESTRATION the tool layers on top of that fan."""

from dataclasses import dataclass

import pytest

from jbrain.agent.briefs import FEED_OPEN
from jbrain.agent.deep_research import (
    DR_MAX_GAP_QUESTIONS,
    DeepResearchService,
)
from jbrain.agent.loop import ToolContext
from jbrain.agent.spawn import _ChildResult
from jbrain.agent.tree import MAX_CHILDREN_PER_PARENT, MAX_DEPTH, TreeState
from jbrain.db.session import SessionContext


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _Result:
    text: str
    usage: _Usage
    parsed: dict | None = None


class _FakeRouter:
    """Scripts the plan / reflect / synthesize(+revise) one-shots by matching the
    persona marker in each prompt's system text, so the assertions never depend on
    call order. Records every call for inspection."""

    def __init__(
        self,
        *,
        complexity: str = "deep",
        sub_questions: tuple[str, ...] = ("sub one", "sub two", "sub three"),
        covered: bool = False,
        gaps: tuple[str, ...] = ("gap one", "gap two"),
    ) -> None:
        self.complexity = complexity
        self.sub_questions = list(sub_questions)
        self.covered = covered
        self.gaps = list(gaps)
        self.calls: list[dict] = []
        self.synth_calls: list[str] = []

    async def complete(self, task, *, system, user_text, json_schema=None, **kw):  # noqa: ANN001
        self.calls.append({"system": system, "user_text": user_text, "json_schema": json_schema})
        usage = _Usage(10, 20)
        if "PLANNER" in system:
            return _Result(
                text="",
                usage=usage,
                parsed={
                    "complexity": self.complexity,
                    "sub_questions": self.sub_questions,
                    "sections": ["Overview", "Detail"],
                },
            )
        if "COVERAGE CHECK" in system:
            return _Result(
                text="", usage=usage, parsed={"covered": self.covered, "gaps": self.gaps}
            )
        # WRITER — the initial synthesis or the post-critique revision.
        self.synth_calls.append(user_text)
        revising = "Critique of your earlier draft" in user_text
        return _Result(text="REVISED REPORT" if revising else "DRAFT REPORT", usage=usage)


class _FakeSpawn:
    """Stands in for SpawnService.run_research_fan: records each fan and returns one ok
    finding per brief (or a scripted failure), so the orchestration's fan calls are
    observable without minting sessions or running a loop."""

    def __init__(self, *, gather_ok: bool = True, review_ok: bool = True) -> None:
        self.fans: list[dict] = []
        self.gather_ok = gather_ok
        self.review_ok = review_ok

    async def run_research_fan(
        self, ctx, *, briefs, persona="research", effort=None, max_parallel=None
    ):  # noqa: ANN001
        briefs = list(briefs)
        self.fans.append({"persona": persona, "briefs": briefs, "effort": effort})
        ok = self.review_ok if persona == "review" else self.gather_ok
        return [
            _ChildResult(
                label=label,
                persona=persona,
                summary=f"{persona} finding for {label}" if ok else "",
                ok=ok,
                session_id=f"sess-{i}",
            )
            for i, (label, _brief) in enumerate(briefs)
        ]


def _ctx(*, depth: int = 0, tree: TreeState | None = None) -> ToolContext:
    return ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=depth,
        agent_tools=frozenset({"deep_research"}),
        tree=tree if tree is not None else TreeState.rooted(800_000),
        run_id="parent-run",
    )


def _svc(router: _FakeRouter, spawn: _FakeSpawn) -> DeepResearchService:
    return DeepResearchService(router=router, spawn=spawn)  # type: ignore[arg-type]


def _research_fans(spawn: _FakeSpawn) -> list[dict]:
    return [f for f in spawn.fans if f["persona"] == "research"]


def _review_fans(spawn: _FakeSpawn) -> list[dict]:
    return [f for f in spawn.fans if f["persona"] == "review"]


# --- refusal paths (return before any model/fan touch) ----------------------


async def test_refused_for_a_child_turn() -> None:
    router, spawn = _FakeRouter(), _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(depth=MAX_DEPTH), {"question": "x"})
    assert "refused" in out.lower()
    assert not spawn.fans and not router.calls


async def test_refused_without_a_tree() -> None:
    router, spawn = _FakeRouter(), _FakeSpawn()
    ctx = ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="s",
        depth=0,
        agent_tools=frozenset({"deep_research"}),
        tree=None,
        run_id="r",
    )
    out = await _svc(router, spawn).research(ctx, {"question": "x"})
    assert "refused" in out.lower()
    assert not spawn.fans and not router.calls


async def test_refused_for_an_empty_question() -> None:
    router, spawn = _FakeRouter(), _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "   "})
    assert "refused" in out.lower()
    assert not spawn.fans and not router.calls


# --- the full deep run ------------------------------------------------------


async def test_deep_run_full_sequence() -> None:
    """complexity=deep runs plan → gather → reflect → refill → synthesize → critique →
    revise, in that shape: one gather fan, one refill fan, one review (critique) fan,
    and the synthesis runs twice (draft + revision)."""
    router = _FakeRouter(complexity="deep", covered=False, gaps=("gap one", "gap two"))
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "how does X work?"})

    research = _research_fans(spawn)
    assert len(research) == 2  # gather + one refill (never a third round)
    assert research[0]["briefs"][0][1] == "sub one"  # gather works the plan's sub-questions
    assert [b[1] for b in research[1]["briefs"]] == ["gap one", "gap two"]  # refill works the gaps
    assert len(_review_fans(spawn)) == 1  # the critique hop
    assert len(router.synth_calls) == 2  # draft + revise
    assert "REVISED REPORT" in out  # the revised draft is what ships
    assert "revised after critique" in out
    assert "complexity: deep" in out


async def test_findings_are_fed_as_bounded_data() -> None:
    """The gathered summaries reach the synthesizer inside the data/instruction
    boundary (the feeding-waves envelope), never as bare prose."""
    router, spawn = _FakeRouter(), _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert any(FEED_OPEN in uw for uw in router.synth_calls)


# --- the complexity skip matrix (narrow-only) -------------------------------


async def test_simple_skips_reflect_refill_and_critique() -> None:
    router = _FakeRouter(complexity="simple", sub_questions=("just one",))
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "what is X?"})
    assert len(_research_fans(spawn)) == 1  # gather only — no refill
    assert not _review_fans(spawn)  # no critique
    assert not any("COVERAGE CHECK" in c["system"] for c in router.calls)  # no reflect call
    assert len(router.synth_calls) == 1  # one synthesis, no revision
    assert "complexity: simple" in out
    assert "revised" not in out


async def test_comparative_gathers_but_skips_gap_round_and_critique() -> None:
    router = _FakeRouter(complexity="comparative")
    spawn = _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "X vs Y vs Z"})
    assert len(_research_fans(spawn)) == 1  # a broad gather, but no refill
    assert not _review_fans(spawn)
    assert not any("COVERAGE CHECK" in c["system"] for c in router.calls)
    assert len(router.synth_calls) == 1


async def test_covered_reflection_skips_the_refill_round() -> None:
    """A deep run whose reflection judges coverage sufficient runs no refill fan, but
    still synthesizes and (deep) critiques."""
    router = _FakeRouter(complexity="deep", covered=True, gaps=())
    spawn = _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "how does X work?"})
    assert len(_research_fans(spawn)) == 1  # gather only; reflect said covered
    assert any("COVERAGE CHECK" in c["system"] for c in router.calls)  # reflect DID run
    assert len(_review_fans(spawn)) == 1  # critique still runs for a deep run


# --- the bound: never a third round; refill children are capped -------------


async def test_refill_children_capped_and_never_a_third_round() -> None:
    """Even when reflection returns many gaps, the refill fan is capped to the gap
    budget AND to the per-run child cap, and there is never a third gather round."""
    router = _FakeRouter(
        complexity="deep",
        sub_questions=tuple(f"sub {i}" for i in range(5)),  # a wide gather
        covered=False,
        gaps=tuple(f"gap {i}" for i in range(6)),  # far more gaps than allowed
    )
    spawn = _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "deep q"})
    research = _research_fans(spawn)
    assert len(research) == 2  # gather + exactly one refill — never three
    gather_n, refill_n = len(research[0]["briefs"]), len(research[1]["briefs"])
    assert refill_n <= DR_MAX_GAP_QUESTIONS
    assert gather_n + refill_n <= MAX_CHILDREN_PER_PARENT  # the per-run child ceiling holds


async def test_bad_complexity_defaults_to_deep_but_cannot_exceed_the_ceiling() -> None:
    """A malformed/injected complexity fails toward the full machine (thorough), but the
    skip matrix only ever removes work — it can never widen past two rounds + critique."""
    router = _FakeRouter(complexity="ULTRA-DEEP-RUN-FOREVER")  # not a valid tier
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert "complexity: deep" in out  # clamped to the strongest real tier
    assert len(_research_fans(spawn)) <= 2  # still at most two rounds


# --- budget + degraded paths ------------------------------------------------


async def test_orchestration_calls_charge_the_tree_budget() -> None:
    tree = TreeState.rooted(800_000)
    before = tree.spent
    router, spawn = _FakeRouter(), _FakeSpawn()
    await _svc(router, spawn).research(_ctx(tree=tree), {"question": "q"})
    # plan + reflect + 2 synth calls each charged 30 tokens (10 in + 20 out).
    assert tree.spent >= before + 4 * 30


async def test_refill_skipped_loud_when_budget_cannot_seat_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = TreeState.rooted(800_000)
    monkeypatch.setattr(tree, "can_admit_budget", lambda n: False)
    router = _FakeRouter(complexity="deep", covered=False, gaps=("gap one",))
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(tree=tree), {"question": "q"})
    assert len(_research_fans(spawn)) == 1  # gather only — refill refused for budget
    assert "coverage may be partial" in out


async def test_no_usable_findings_is_a_clean_refusal() -> None:
    router, spawn = _FakeRouter(), _FakeSpawn(gather_ok=False)
    out = await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert "refused" in out.lower()
    assert not router.synth_calls  # never synthesizes over nothing


async def test_empty_critique_skips_the_revision() -> None:
    router = _FakeRouter(complexity="deep", covered=True, gaps=())
    spawn = _FakeSpawn(review_ok=False)  # the critique child fails → empty critique
    out = await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert len(router.synth_calls) == 1  # no revision pass
    assert "revised" not in out
