"""The deep_research v2 state machine (docs/plans/DEEP_RESEARCH_TOOL_PLAN.md): the
plan → gather → analyze → reflect → (refill) → synthesize → critique/revise pipeline,
which ALWAYS orchestrates when invoked (complexity only sizes gather breadth, never
skips a stage), the cross-agent analyst hand-off, the visible phase events, the fixed
gap-round bound, budget charging, and the refusal paths — all proven with fakes (a
scripted router + a fake fan), no DB, no real model. The security-critical fan clamps
live in test_spawn.py; here we assert the ORCHESTRATION the tool layers on the fan."""

from dataclasses import dataclass

import pytest

from jbrain.agent.briefs import FEED_OPEN
from jbrain.agent.contracts import WebSource
from jbrain.agent.deep_research import (
    DR_CRITIQUE_RESERVE,
    DR_MAX_GAP_QUESTIONS,
    DR_REVIEW_RESERVE,
    DR_SIMPLE_BREADTH,
    DeepResearchService,
)
from jbrain.agent.loop import ToolContext
from jbrain.agent.spawn import _ChildResult
from jbrain.agent.tree import MAX_DEPTH, TreeState
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
    call order. Records every call for inspection. (The analyst + critique are review
    CHILDREN, handled by _FakeSpawn, not the router.)"""

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
    """Stands in for SpawnService.run_research_fan: records each fan and returns one
    finding per brief, with per-stage success/failure and a refusal switch, so the
    orchestration's degradation paths are observable without minting sessions or a loop.

    Stage routing: a `research` fan is the gather (fan 1) then the refill (fan 2+); a
    `review` fan is the analyst (label "cross-check") or the critique (label "critique").
    `refuse_labels` makes a fan whose first brief carries that label return `[]` — the
    real `run_research_fan`'s admission-refused path (tree total / budget), which the
    default fake could never exercise."""

    def __init__(
        self,
        *,
        gather_ok: bool = True,
        refill_ok: bool = True,
        analyst_ok: bool = True,
        critique_ok: bool = True,
        refuse_labels: frozenset[str] = frozenset(),
    ) -> None:
        self.fans: list[dict] = []
        self.gather_ok = gather_ok
        self.refill_ok = refill_ok
        self.analyst_ok = analyst_ok
        self.critique_ok = critique_ok
        self.refuse_labels = set(refuse_labels)
        self._research_fans = 0

    async def run_research_fan(
        self, ctx, *, briefs, persona="research", effort=None, max_parallel=None
    ):  # noqa: ANN001
        briefs = list(briefs)
        # Snapshot the reserve carved off the pool at the instant this fan runs, so the
        # staging (gather → analyst → critique step-down) is observable without a loop.
        self.fans.append(
            {
                "persona": persona,
                "briefs": briefs,
                "effort": effort,
                "stage_reserve": ctx.tree.stage_reserve if ctx.tree else None,
            }
        )
        first_label = briefs[0][0] if briefs else ""
        if first_label in self.refuse_labels:
            return []  # admission refused (tree total / budget) → the caller degrades
        if persona == "review":
            ok = self.analyst_ok if first_label == "cross-check" else self.critique_ok
        else:
            self._research_fans += 1
            ok = self.gather_ok if self._research_fans == 1 else self.refill_ok
        return [
            _ChildResult(
                label=label,
                persona=persona,
                summary=f"{persona} finding for {label}" if ok else "",
                ok=ok,
                session_id=f"sess-{i}",
                # A research child reaches a real page; the URL rides up so the run can
                # build its global citation registry (favicon targets).
                web_sources=(
                    (WebSource(url=f"https://ex.com/{label}", title=f"Src {label}"),)
                    if ok and persona == "research"
                    else ()
                ),
            )
            for i, (label, _brief) in enumerate(briefs)
        ]


def _ctx(
    *, depth: int = 0, tree: TreeState | None = None, events: list | None = None
) -> ToolContext:
    return ToolContext(
        session=SessionContext(principal_id="p1", principal_kind="owner"),
        scopes=(),
        agent_session_id="parent-sess",
        depth=depth,
        agent_tools=frozenset({"deep_research"}),
        tree=tree if tree is not None else TreeState.rooted(800_000),
        run_id="parent-run",
        emit_event=(events.append if events is not None else None),
    )


def _svc(router: _FakeRouter, spawn: _FakeSpawn) -> DeepResearchService:
    return DeepResearchService(router=router, spawn=spawn)  # type: ignore[arg-type]


def _research_fans(spawn: _FakeSpawn) -> list[dict]:
    return [f for f in spawn.fans if f["persona"] == "research"]


def _review_fans(spawn: _FakeSpawn) -> list[dict]:
    return [f for f in spawn.fans if f["persona"] == "review"]


def _reflected(router: _FakeRouter) -> bool:
    return any("COVERAGE CHECK" in c["system"] for c in router.calls)


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


# --- the full run: every stage runs when invoked ----------------------------


async def test_full_run_orchestrates_every_stage() -> None:
    """A run drives plan → gather → analyze → reflect → refill → synthesize → critique →
    revise: two research fans (gather + one refill), TWO review fans (the analyst
    cross-check + the critique), and two synthesis calls (draft + revise)."""
    router = _FakeRouter(complexity="deep", covered=False, gaps=("gap one", "gap two"))
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "how does X work?"})

    research = _research_fans(spawn)
    review = _review_fans(spawn)
    assert len(research) == 2  # gather + one refill (never a third round)
    assert research[0]["briefs"][0][1] == "sub one"  # gather works the plan's sub-questions
    assert [b[1] for b in research[1]["briefs"]] == ["gap one", "gap two"]  # refill works the gaps
    assert len(review) == 2  # the analyst cross-check AND the critique
    assert review[0]["briefs"][0][0] == "cross-check"  # analyst runs first
    assert review[1]["briefs"][0][0] == "critique"  # critique runs last
    assert _reflected(router)  # the coverage check ran
    assert len(router.synth_calls) == 2  # draft + revise
    assert "REVISED REPORT" in out
    assert "cross-checked" in out and "revised after critique" in out


async def test_analyst_is_fed_the_gather_findings_before_synthesis() -> None:
    """The cross-agent hand-off: the analyst review child's brief carries the gather
    summaries as boundary-wrapped data, and it runs before the report is written."""
    router, spawn = _FakeRouter(), _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "q"})
    analyst = _review_fans(spawn)[0]
    assert analyst["briefs"][0][0] == "cross-check"
    assert FEED_OPEN in analyst["briefs"][0][1]  # fed the findings as escaped data


async def test_phases_are_emitted_for_the_owner_to_watch() -> None:
    events: list = []
    router, spawn = _FakeRouter(), _FakeSpawn()
    await _svc(router, spawn).research(_ctx(events=events), {"question": "q"})
    labels = [e.label for e in events if getattr(e, "type", "") == "tool_progress"]
    # The owner sees the run move through its stages, not just "two agents spawned".
    assert any("Planning" in x for x in labels)
    assert any("Researching" in x for x in labels)
    assert any("Cross-checking" in x for x in labels)
    assert any("Checking coverage" in x for x in labels)
    assert any("Writing" in x for x in labels)
    assert any("Reviewing" in x for x in labels)


async def test_run_emits_the_report_view() -> None:
    """The tool result carries the registered `deep_research_report` view, with the
    report Markdown, the provenance counts, and the sub-agent roster (data only)."""
    router = _FakeRouter(complexity="deep", covered=False, gaps=("gap one",))
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "how does X work?"})
    view = out.view  # type: ignore[attr-defined]
    assert view is not None and view.view == "deep_research_report"
    d = view.data
    assert d["complexity"] == "deep"
    assert d["rounds"] == 2 and d["revised"] is True and d["analyzed"] is True
    assert "REVISED REPORT" in d["report_md"]
    # `sub_agents` counts the research FINDINGS (3 gather + 1 refill); the roster ALSO
    # carries the analyst + critique review children so the reopened report shows who ran.
    assert d["sub_agents"] == 4
    personas = [c["persona"] for c in d["children"]]
    assert personas.count("research") == 4 and personas.count("review") == 2
    labels = [c["label"] for c in d["children"]]
    assert "cross-check" in labels and "critique" in labels
    assert all("session_id" in c for c in d["children"])


async def test_findings_are_fed_as_bounded_data() -> None:
    """The gathered summaries reach the synthesizer inside the data/instruction
    boundary (the feeding-waves envelope), never as bare prose."""
    router, spawn = _FakeRouter(), _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert any(FEED_OPEN in uw for uw in router.synth_calls)


async def test_citations_are_tracked_from_sub_agents_to_the_report() -> None:
    """The children's real URLs are collected into a global source registry: the
    synthesizer is given the numbered SOURCES list to cite against, and the report view
    carries the same web_sources so `[^n]` renders as tappable favicons — the citations
    are not lost between the sub-agents and the final report."""
    router = _FakeRouter(complexity="deep", covered=True, gaps=())
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "how does X work?"})
    # The synthesizer is handed the canonical numbered source list with the real URLs.
    assert any("SOURCES — cite with these exact numbers" in uw for uw in router.synth_calls)
    assert any("https://ex.com/" in uw for uw in router.synth_calls)
    # The report view carries the favicon citation registry (url + title), deduped.
    ws = out.view.data["web_sources"]  # type: ignore[attr-defined]
    assert ws and all(s["url"].startswith("https://ex.com/") and s["title"] for s in ws)
    urls = [s["url"] for s in ws]
    assert len(urls) == len(set(urls))  # deduped


# --- complexity sizes breadth only; it never skips a stage ------------------


async def test_simple_narrows_breadth_but_still_orchestrates() -> None:
    """A `simple` rating researches fewer angles, but the analyst, coverage check, and
    critique all still run — the tool always does the full orchestration when invoked."""
    router = _FakeRouter(
        complexity="simple", sub_questions=("a", "b", "c", "d"), covered=True, gaps=()
    )
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "what is X?"})
    gather = _research_fans(spawn)[0]
    assert len(gather["briefs"]) <= DR_SIMPLE_BREADTH  # breadth narrowed
    assert _reflected(router)  # coverage check STILL ran
    assert len(_review_fans(spawn)) == 2  # analyst + critique STILL ran
    assert "complexity: simple" in out


async def test_comparative_uses_full_breadth_and_still_orchestrates() -> None:
    router = _FakeRouter(complexity="comparative", covered=True, gaps=())
    spawn = _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "X vs Y vs Z"})
    assert len(_research_fans(spawn)[0]["briefs"]) == 3  # full planned breadth
    assert _reflected(router)
    assert len(_review_fans(spawn)) == 2  # analyst + critique


async def test_covered_reflection_skips_only_the_refill_round() -> None:
    """When reflection judges coverage sufficient there is no refill fan, but the
    analyst, synthesis, and critique all still run."""
    router = _FakeRouter(complexity="deep", covered=True, gaps=())
    spawn = _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "how does X work?"})
    assert len(_research_fans(spawn)) == 1  # gather only; reflect said covered
    assert _reflected(router)
    assert len(_review_fans(spawn)) == 2  # analyst + critique still run


# --- the bound: never a third round; refill children are capped -------------


async def test_refill_children_capped_and_never_a_third_round() -> None:
    router = _FakeRouter(
        complexity="deep",
        sub_questions=tuple(f"sub {i}" for i in range(5)),
        covered=False,
        gaps=tuple(f"gap {i}" for i in range(6)),  # far more gaps than allowed
    )
    spawn = _FakeSpawn()
    await _svc(router, spawn).research(_ctx(), {"question": "deep q"})
    research = _research_fans(spawn)
    assert len(research) == 2  # gather + exactly one refill — never three
    assert len(research[1]["briefs"]) <= DR_MAX_GAP_QUESTIONS


async def test_bad_complexity_defaults_to_deep_and_runs_full_pipeline() -> None:
    router = _FakeRouter(complexity="ULTRA-DEEP-RUN-FOREVER")  # not a valid tier
    spawn = _FakeSpawn()
    out = await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert "complexity: deep" in out  # clamped to the strongest real tier
    assert len(_research_fans(spawn)) <= 2  # still at most two gather rounds
    assert len(_review_fans(spawn)) == 2  # full orchestration


# --- planner prompt guard ---------------------------------------------------


def test_plan_prompt_forbids_meta_and_cross_child_subquestions() -> None:
    """Regression guard for the planner fix (the 1918-flu 'Create a citation matrix for
    all sources gathered in the previous three sub-questions' angle, which an isolated
    parallel child could never satisfy): the prompt must forbid process/meta tasks and
    any dependence on a sibling's answer, and steer toward fewer angles."""
    from jbrain.agent.deep_research import _PLAN

    body = _PLAN.body.lower()
    assert _PLAN.version == "dr-plan-v2"
    assert "citation matrix" in body  # the exact meta task that leaked through v1
    assert "process or meta task" in body
    assert "in isolation" in body  # names why cross-child briefs can't work
    assert "fewer" in body  # the anti-over-decomposition steer


# --- budget + degraded paths ------------------------------------------------


async def test_review_children_get_a_reserved_budget_slice() -> None:
    """The post-gather review children are protected from a greedy gather round: the
    gather fan runs with the full review reserve carved off the pool, the analyst then
    runs with only the critique's slice still reserved, and the critique runs with the
    reserve fully released — and the reserve is restored on exit so a later fan in this
    turn isn't gated (the 1918-flu failure: gather drained the pool, analyst was starved)."""
    router, spawn = _FakeRouter(), _FakeSpawn()
    tree = TreeState.rooted(800_000)
    assert tree.stage_reserve == 0
    await _svc(router, spawn).research(_ctx(tree=tree), {"question": "q"})

    gather = _research_fans(spawn)[0]
    assert gather["stage_reserve"] == DR_REVIEW_RESERVE  # full slice held through gather
    analyst = next(f for f in _review_fans(spawn) if f["briefs"][0][0] == "cross-check")
    assert analyst["stage_reserve"] == DR_CRITIQUE_RESERVE  # analyst gets the rest
    critique = next(f for f in _review_fans(spawn) if f["briefs"][0][0] == "critique")
    assert critique["stage_reserve"] == 0  # released once the draft is written
    assert tree.stage_reserve == 0  # restored on exit


async def test_review_reserve_is_restored_even_when_gather_yields_nothing() -> None:
    """The early no-findings refusal still unwinds the reserve (it runs in a `finally`),
    so a follow-up fan in the same turn sees a clean pool."""
    router, spawn = _FakeRouter(), _FakeSpawn(gather_ok=False)
    tree = TreeState.rooted(800_000)
    out = await _svc(router, spawn).research(_ctx(tree=tree), {"question": "q"})
    assert "refused" in out.lower()
    assert tree.stage_reserve == 0  # reserve unwound despite the early return


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


async def test_failed_critique_skips_revision_but_keeps_the_analysis() -> None:
    """A failed critique child skips the revision, but a successful analyst still leaves
    the run cross-checked — the two review stages degrade independently."""
    router = _FakeRouter(complexity="deep", covered=True, gaps=())
    spawn = _FakeSpawn(analyst_ok=True, critique_ok=False)
    out = await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert len(router.synth_calls) == 1  # no revision pass
    assert "revised" not in out
    assert "cross-checked" in out  # the analyst still ran and succeeded


async def test_analyst_refusal_degrades_to_no_cross_check() -> None:
    """When the analyst fan is refused (admission), the run still completes — it just
    isn't cross-checked; synthesis and critique still run."""
    router = _FakeRouter(complexity="deep", covered=True, gaps=())
    spawn = _FakeSpawn(refuse_labels=frozenset({"cross-check"}))
    out = await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert out.view is not None and out.view.data["analyzed"] is False  # type: ignore[attr-defined]
    assert "cross-checked" not in out
    assert router.synth_calls  # the report was still written
    # The critique fan (a separate review fan) still ran.
    assert any(f["briefs"][0][0] == "critique" for f in _review_fans(spawn))


async def test_refill_that_produces_nothing_is_reported_partial_not_a_second_round() -> None:
    """A refill that IS admitted but whose gap children all fail added no coverage: the
    report is flagged partial and does NOT claim a second round."""
    router = _FakeRouter(complexity="deep", covered=False, gaps=("gap one",))
    spawn = _FakeSpawn(refill_ok=False)  # gather ok; the gap child fails
    out = await _svc(router, spawn).research(_ctx(), {"question": "q"})
    assert len(_research_fans(spawn)) == 2  # the refill fan DID run
    assert "coverage may be partial" in out
    assert out.view is not None and out.view.data["rounds"] == 1  # type: ignore[attr-defined]
