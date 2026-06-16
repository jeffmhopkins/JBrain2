"""The live eval `Scorer` for the `eval_run` action (docs/ASSISTANT.md
"Self-improvement loops", docs/WORKFLOW_ENGINE_PLAN.md §5 Track C).

`evalaction.py` injects a `Scorer` — `(suite, version_label) -> (EvalRun, tokens)`
— at wiring time. This module builds the LIVE one: it drives the note.extract eval
suite (`backend/evals/run.py`'s cases) through the **LLM adapter** (the router,
never a provider SDK), scores the model's own output with the existing
`eval_run_from_cases` adapter (preserving the two-dimensional `{task, safety}`
split the promotion gate depends on), and returns the run plus the total tokens
billed — the real spend the budget gate is then charged.

CI never builds this: the worker wires it only when given a router, and the tests
inject a deterministic fake scorer (no model), exactly as the LLM adapter is faked
everywhere else. Keeping the live scorer behind a factory here — not in
`evalaction.py` — is what lets the action module stay free of any live router or
`evals/run.py` import (it depends only on the abstract `Scorer` callable).
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

    The dev eval RUNNER (`evals/run.py`) is imported lazily, inside the callable —
    NOT at module top level — so importing this module (and therefore `worker`/
    `main`) never requires the dev-only `evals/` package, which the container image
    does not ship. The runner is only needed when an `eval_run` job actually fires;
    a deployment without the harness surfaces a graceful `PermanentJobError` rather
    than a bare `ModuleNotFoundError` (eval_run is opt-in and on no prod schedule)."""

    async def scorer(suite: str, version_label: str) -> tuple[EvalRun, int]:
        runner = _load_runner()
        cases = _select_cases(suite)
        results, tokens = await runner.score_cases(router, cases)
        return runner.eval_run_from_cases(results, version_label), tokens

    return scorer


def _load_runner() -> Any:
    """Import the dev eval runner module on demand, translating its absence into a
    `PermanentJobError` — the harness is dev/CI-only and not in the shipped image,
    so an `eval_run` job in such a deployment fails cleanly instead of crashing."""
    try:
        import evals.run as runner
    except (ModuleNotFoundError, ImportError) as exc:
        raise PermanentJobError(
            "eval harness not available in this deployment (evals/ is dev/CI-only)"
        ) from exc
    return runner


def _select_cases(suite: str) -> list[dict[str, Any]]:
    """The cases a `suite` label runs. Empty / `all` is the whole curated set;
    anything else is a name-substring filter so an operator can score one slice
    (e.g. `temporal`) without a separate fixture file.

    Loads cases via the lazily-imported dev runner so this module imports clean
    without `evals/` (the gap that crash-looped the deploy); a deployment lacking
    the harness gets a graceful `PermanentJobError` from `_load_runner`."""
    cases = _load_runner().load_cases()
    selector = suite.strip().lower()
    if selector in ("", "all"):
        return cases
    return [c for c in cases if selector in c["name"].lower()]


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
    asserts an exact six-row set, and the seed projection is the deferred follow-up
    (H·B)."""
    action = EvalRunAction(
        scorer=scorer,
        store=EvalRunStore(maker),
        settings=SqlSettingsStore(maker),
        ctx=SYSTEM_CTX,
    )
    return action.run
