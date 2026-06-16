"""The live eval `Scorer` for the `eval_run` action (docs/ASSISTANT.md
"Self-improvement loops", docs/WORKFLOW_ENGINE_PLAN.md §5 Track C).

`evalaction.py` injects a `Scorer` — `(suite, version_label) -> (EvalRun, tokens)`
— at wiring time. This module builds the LIVE one: it drives the note.extract eval
suite (`jbrain.evals.runner`'s cases) through the **LLM adapter** (the router,
never a provider SDK), scores the model's own output with the existing
`eval_run_from_cases` adapter (preserving the two-dimensional `{task, safety}`
split the promotion gate depends on), and returns the run plus the total tokens
billed — the real spend the budget gate is then charged.

CI never builds this: the worker wires it only when given a router, and the tests
inject a deterministic fake scorer (no model), exactly as the LLM adapter is faked
everywhere else. Keeping the live scorer behind a factory here — not in
`evalaction.py` — is what lets the action module stay free of any live router or
`jbrain.evals.runner` import (it depends only on the abstract `Scorer` callable).
The runner + its corpus ship IN the `jbrain` package (Phase-5 Track H·B), so the
nightly `eval_run` schedule scores the live model in production.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.settings_store import SqlSettingsStore
from jbrain.workflow.evalaction import EvalRunAction, Scorer
from jbrain.workflow.evalstore import EvalRunStore
from jbrain.workflow.promotion import EvalRun


def build_live_scorer(router: Any) -> Scorer:
    """A live `Scorer` bound to `router` (the LLM adapter). The returned callable
    runs the eval suite's cases through the model and reports `(EvalRun, tokens)`.

    `suite` selects the cases: a substring filter over case names, or `""`/`"all"`
    for the whole set (the nightly default). `version_label` is recorded as the
    run's version so a later candidate is gated against this stored baseline.

    The runner (`jbrain.evals.runner`) ships in the package and is imported lazily,
    inside the callable — NOT at module top level — purely to keep this module's
    import cheap (it pulls the analysis/prompt stack only when a job actually fires)."""

    async def scorer(suite: str, version_label: str) -> tuple[EvalRun, int]:
        runner = _load_runner()
        cases = _select_cases(suite)
        results, tokens = await runner.score_cases(router, cases)
        return runner.eval_run_from_cases(results, version_label), tokens

    return scorer


def _load_runner() -> Any:
    """The in-package eval runner. Imported on demand (not at module top) so the
    import is lazy, but it is a hard in-package dependency — it ships in the image,
    so there is no "missing harness" branch to handle."""
    import jbrain.evals.runner as runner

    return runner


def _select_cases(suite: str) -> list[dict[str, Any]]:
    """The cases a `suite` label runs. Empty / `all` is the whole curated set;
    anything else is a name-substring filter so an operator can score one slice
    (e.g. `temporal`) without a separate fixture file.

    Fail-closed if the selection is empty: scoring zero cases would silently store a
    contentless `EvalRun`, so a missing corpus or a filter that matches nothing is a
    `PermanentJobError` (no retry — re-running cannot conjure cases)."""
    cases = _load_runner().load_cases()
    selector = suite.strip().lower()
    selected = (
        cases if selector in ("", "all") else [c for c in cases if selector in c["name"].lower()]
    )
    if not selected:
        raise PermanentJobError(f"eval suite {suite!r} matched no cases")
    return selected


def eval_run_handler(
    maker: async_sessionmaker[AsyncSession], scorer: Scorer
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """The `eval_run` action wrapped as a payload-only queue handler, bound to a
    `scorer` (the live one in the worker, a fake in CI). The handler gate→score→
    store→charge sequence lives in `EvalRunAction`; this just binds it to the
    owner-scoped `SYSTEM_CTX` (`eval_runs` is owner-only audit metadata) and the
    settings/store off the worker's maker, then exposes the bound `.run`.

    Kept OUT of `ACTION_SPECS`/the `app.actions` seed (the worker composes
    `EVAL_RUN_SPEC` into the registry like `PURGE_ACTION`): the 0035 seed-lockstep
    asserts an exact six-row set. The nightly schedule (migration 0044) references
    `eval_run` by name through the in-code registry — exactly as the seeded sweeps do
    for `PURGE_ACTION` / the reconcilers — so it needs no `app.actions` row (H·B)."""
    action = EvalRunAction(
        scorer=scorer,
        store=EvalRunStore(maker),
        settings=SqlSettingsStore(maker),
        ctx=SYSTEM_CTX,
    )
    return action.run
