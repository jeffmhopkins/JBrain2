"""Regression guard: the shipped `jbrain` package must boot without the dev-only
`evals/` package on the path.

The container image ships only `src/jbrain` — `backend/evals/` (the eval runner +
fixtures) is NOT copied. A top-level `from evals...` in any shipped module therefore
crash-loops api/worker with `ModuleNotFoundError: No module named 'evals'` at import
time. CI runs with `evals/` on `sys.path`, so it never caught this. This test hides
the package entirely (an import hook that raises for `evals` and `evals.*`) and proves
that importing `jbrain.workflow.eval_scorer`, `jbrain.worker`, and `jbrain.main`
(building the app) all succeed regardless — and that an `eval_run` job fired without
the harness fails GRACEFULLY with `PermanentJobError`, never a bare `ModuleNotFoundError`.
"""

from __future__ import annotations

import builtins
import importlib
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from jbrain.queue import PermanentJobError

_HIDDEN = "evals"


def _is_hidden(name: str) -> bool:
    return name == _HIDDEN or name.startswith(_HIDDEN + ".")


class _HideEvalsFinder:
    """A `sys.meta_path` finder that makes `evals`/`evals.*` look uninstalled —
    `find_spec` returning a spec whose loader raises is how Python reports a module
    that exists in the table but cannot be imported."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if _is_hidden(fullname):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


@pytest.fixture
def evals_hidden() -> Iterator[None]:
    """Hide the `evals` package: drop any cached copies, install a meta-path finder
    that raises for it, and also block the `__import__` fast path (which short-circuits
    meta_path for already-imported parents). Shipped modules are re-imported fresh so
    their top-level imports run under the hidden state. Everything is restored after."""
    finder = _HideEvalsFinder()
    saved_modules = {n: m for n, m in sys.modules.items() if _is_hidden(n)}
    # Re-import the shipped modules under test from scratch so their module-level
    # imports actually execute while evals is hidden (a cached module would not).
    reimport = [
        n
        for n in list(sys.modules)
        if n == "jbrain.main" or n == "jbrain.worker" or n.startswith("jbrain.workflow.eval")
    ]
    saved_reimport = {n: sys.modules[n] for n in reimport}

    for n in (*saved_modules, *reimport):
        sys.modules.pop(n, None)

    real_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if _is_hidden(name):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    sys.meta_path.insert(0, finder)
    builtins.__import__ = guarded_import
    try:
        yield
    finally:
        builtins.__import__ = real_import
        sys.meta_path.remove(finder)
        for n in (*reimport, *saved_modules):
            sys.modules.pop(n, None)
        sys.modules.update(saved_modules)
        sys.modules.update(saved_reimport)


def test_evals_is_actually_hidden(evals_hidden: None) -> None:
    """Sanity: the fixture genuinely makes `evals` unimportable, so the assertions
    below are meaningful (a no-op fixture would make this whole test vacuous)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("evals.promotion")


def test_shipped_modules_import_without_evals(evals_hidden: None) -> None:
    """The boot path the container actually runs: importing the scorer, the worker,
    and the api app must NOT require `evals`. This is the exact failure that took the
    deploy down."""
    importlib.import_module("jbrain.workflow.eval_scorer")
    importlib.import_module("jbrain.worker")
    main = importlib.import_module("jbrain.main")
    # Building the app/registry must also work — `app = create_app()` runs at import,
    # but exercise it explicitly to cover the action registry composition too.
    assert main.create_app() is not None


def test_promotion_gate_still_imports_from_jbrain(evals_hidden: None) -> None:
    """The production gate types now live in `jbrain.workflow.promotion` (stdlib only),
    so they remain importable with the dev harness gone."""
    promotion = importlib.import_module("jbrain.workflow.promotion")
    assert hasattr(promotion, "EvalRun")
    assert hasattr(promotion, "promotion_decision")


async def test_live_scorer_fails_gracefully_without_harness(evals_hidden: None) -> None:
    """Firing the live scorer (what an `eval_run` job invokes) without the harness must
    raise the graceful `PermanentJobError`, NOT a bare `ModuleNotFoundError` — the worker
    fails the opt-in job cleanly instead of crash-looping."""
    eval_scorer = importlib.import_module("jbrain.workflow.eval_scorer")
    scorer = eval_scorer.build_live_scorer(router=object())
    with pytest.raises(PermanentJobError):
        await scorer("all", "v-test")
