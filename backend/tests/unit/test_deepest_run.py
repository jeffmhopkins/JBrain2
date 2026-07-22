"""The trusted deepest-run context builder (DEEPEST_RESEARCH_TOOL_PLAN.md, R4): the one
place a background deepest run's context is assembled. Pure, DB-free — the assertions are
the security properties of that context: owner-scoped but KB-less, the only max_depth>1
mint, no location, and the clamp ceiling a research_deep task agent needs."""

from types import SimpleNamespace

from jbrain.agent.deepest_run import (
    DEEPEST_DEFAULT_CEILING_TOKENS,
    DEEPEST_DEFAULT_WALL_CLOCK_S,
    build_deepest_run_context,
)
from jbrain.agent.tree import DEEPEST_MAX_DEPTH, MAX_DEPTH


def test_context_is_owner_scoped_but_kb_less() -> None:
    """Owner identity (so it can mint child sessions and cite) but EMPTY domain scopes —
    the orchestrator and its children read no owner-domain data (the health/finance/
    location firewalls never enter the run), exactly like the in-request jerv orchestrator."""
    ctx = build_deepest_run_context("owner-1", agent_session_id="s1", run_id="r1")
    assert ctx.session.principal_id == "owner-1"
    assert ctx.session.principal_kind == "owner"
    assert ctx.session.domain_scopes == ()  # KB-less: cannot read ANY domain, not just cross-domain
    assert ctx.scopes == ()


def test_context_is_the_only_two_tier_mint_and_has_no_location() -> None:
    ctx = build_deepest_run_context("owner-1", agent_session_id="s1", run_id="r1")
    assert ctx.depth == 0
    assert ctx.tree is not None
    assert ctx.tree.max_depth == DEEPEST_MAX_DEPTH > MAX_DEPTH  # the two-tier seed
    assert ctx.here is None and ctx.here_as_of is None  # no location in a background run


def test_context_clamp_ceiling_covers_a_task_agent() -> None:
    """The orchestrator holds what a research_deep task agent must inherit through the
    parent⊆child clamp — decompose_research plus the web tools — else the clamp would
    strip decompose and the second tier could never spawn."""
    ctx = build_deepest_run_context("owner-1", agent_session_id="s1", run_id="r1")
    assert "decompose_research" in ctx.agent_tools
    assert {"web_search", "web_fetch"} <= ctx.agent_tools


def test_ceiling_defaults_apply_and_override() -> None:
    default = build_deepest_run_context("o", agent_session_id="s", run_id="r")
    assert default.tree is not None and default.tree.tree_budget == DEEPEST_DEFAULT_CEILING_TOKENS
    assert DEEPEST_DEFAULT_WALL_CLOCK_S > 0
    custom = build_deepest_run_context(
        "o", agent_session_id="s", run_id="r", budget_tokens=1_000_000, wall_clock_s=60
    )
    assert custom.tree is not None and custom.tree.tree_budget == 1_000_000


# --- the run driver (R7): composes run-state + DeepResearchService + progress ----------

from jbrain.agent.deepest_run import run_deepest  # noqa: E402
from jbrain.db.session import SessionContext  # noqa: E402


class _FakeService:
    """Stands in for DeepResearchService: records the research call, drives the per-round
    hook, and (optionally) fails — so the driver's composition is observable without an LLM."""

    def __init__(self, *, rounds=((1, 3), (2, 5)), boom: bool = False) -> None:
        self.calls: list[dict] = []
        self._rounds = rounds
        self._boom = boom

    async def research(self, ctx, args, *, on_round=None):  # noqa: ANN001, ANN003
        self.calls.append({"args": args, "max_depth": ctx.tree.max_depth if ctx.tree else None})
        if self._boom:
            raise RuntimeError("research blew up")
        if on_round is not None:
            for rn, f in self._rounds:
                await on_round(rn, f)
        return "THE REPORT"


class _FakeRunState:
    """Stands in for the research_run_state repo module."""

    def __init__(self, *, claimable: bool = True, existing: dict | None = None) -> None:
        self.created: list[str] = []
        self.checkpoints: list[dict] = []
        self.finished: list[str] = []
        self.claims: list[str] = []
        self._claimable = claimable
        self._existing = existing  # a stored row `load` returns (for resume)

    def run_state_context(self, principal_id: str) -> SessionContext:
        return SessionContext(
            principal_id=principal_id, principal_kind="owner", domain_scopes=("external",)
        )

    async def create_run(
        self, maker, ctx, *, run_id, session_id, question, ceiling_tokens, wall_clock_deadline
    ):  # noqa: ANN001, ANN003, E501
        self.created.append(run_id)
        return "row-id"

    async def checkpoint(self, maker, ctx, *, run_id, round, spent_tokens, agents_spawned, state):  # noqa: ANN001, ANN003, A002
        self.checkpoints.append({"round": round, "findings": state.get("findings")})
        return True

    async def finish(self, maker, ctx, *, run_id, status):  # noqa: ANN001, ANN003
        self.finished.append(status)
        return True

    async def claim_resume(self, maker, ctx, run_id):  # noqa: ANN001, ANN003
        self.claims.append(run_id)
        return self._claimable

    async def load(self, maker, ctx, run_id):  # noqa: ANN001, ANN003
        return SimpleNamespace(**self._existing) if self._existing else None


class _FakeProgress:
    def __init__(self) -> None:
        self.rounds: list[tuple] = []
        self.dones: list[str] = []

    async def round(self, owner_ctx, *, session_id, run_id, round_no, findings, coverage_label):  # noqa: ANN001, ANN003
        self.rounds.append((round_no, findings, coverage_label))

    async def done(self, owner_ctx, *, session_id, run_id, question):  # noqa: ANN001, ANN003
        self.dones.append(question)


async def test_driver_composes_a_full_run() -> None:
    """Happy path: opens the checkpoint, drives research in DEEPEST mode with a two-tier
    tree, checkpoints + posts progress each committed round, then marks done and announces."""
    svc, rs, prog = _FakeService(), _FakeRunState(), _FakeProgress()
    status = await run_deepest(
        principal_id="owner-1",
        run_id="run-1",
        session_id="sess-1",
        question="how does X actually work",
        maker=object(),
        service=svc,  # type: ignore[arg-type]
        progress=prog,  # type: ignore[arg-type]
        run_state=rs,  # type: ignore[arg-type]
    )
    assert status == "done"
    assert svc.calls[0]["args"]["mode"] == "deepest"
    assert svc.calls[0]["max_depth"] == 2  # the trusted context seeded the two-tier tree
    assert rs.created == ["run-1"]
    assert [c["round"] for c in rs.checkpoints] == [1, 2]  # every committed round checkpointed
    assert rs.finished == ["done"]
    assert [r[0] for r in prog.rounds] == [1, 2]  # per-round progress to the chat
    assert prog.dones == ["how does X actually work"]


async def test_driver_fails_closed_on_a_research_error() -> None:
    """A research failure marks the run failed, posts a failure notice, and never announces
    completion — and it does NOT raise (the lane must not crash)."""
    svc, rs, prog = _FakeService(boom=True), _FakeRunState(), _FakeProgress()
    status = await run_deepest(
        principal_id="owner-1",
        run_id="run-2",
        session_id="sess-2",
        question="q",
        maker=object(),
        service=svc,  # type: ignore[arg-type]
        progress=prog,  # type: ignore[arg-type]
        run_state=rs,  # type: ignore[arg-type]
    )
    assert status == "failed"
    assert rs.finished == ["failed"]
    assert prog.dones == []  # no completion announced
    assert prog.rounds and "failed" in prog.rounds[-1][2]  # a failure notice was posted


# --- resume (R7): claim an interrupted run and re-drive -------------------------------

from jbrain.agent.deepest_run import resume_deepest  # noqa: E402


async def test_resume_claims_then_re_drives() -> None:
    """A restart claims the interrupted run (exactly-once) and re-drives it from the
    checkpoint's rehydrated params — a coverage-equivalent report over the same question."""
    svc = _FakeService()
    rs = _FakeRunState(
        claimable=True,
        existing={
            "status": "running",
            "question": "how does X work",
            "session_id": "sess-1",
            "ceiling_tokens": 50_000_000,
            "wall_clock_deadline": None,
        },
    )
    prog = _FakeProgress()
    status = await resume_deepest(
        principal_id="owner-1",
        run_id="run-1",
        maker=object(),
        service=svc,  # type: ignore[arg-type]
        progress=prog,  # type: ignore[arg-type]
        run_state=rs,  # type: ignore[arg-type]
    )
    assert status == "done"
    assert rs.claims == ["run-1"]  # it claimed the run
    assert svc.calls and svc.calls[0]["args"]["question"] == "how does X work"  # rehydrated
    assert rs.finished == ["done"]


async def test_resume_declines_a_run_it_cannot_claim() -> None:
    """A run already claimed by another process (or finished) yields None — no double-drive."""
    svc = _FakeService()
    rs = _FakeRunState(claimable=False)
    status = await resume_deepest(
        principal_id="owner-1",
        run_id="run-1",
        maker=object(),
        service=svc,  # type: ignore[arg-type]
        progress=_FakeProgress(),  # type: ignore[arg-type]
        run_state=rs,  # type: ignore[arg-type]
    )
    assert status is None
    assert not svc.calls  # never re-driven
