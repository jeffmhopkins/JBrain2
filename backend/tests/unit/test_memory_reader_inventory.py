"""Where the box reads its own memory — pinned, so a new reader cannot appear unnoticed.

Every failure this codebase has had around model loading has the same shape: a second place
that reads memory, decides something, and disagrees with the first. Six budgets were found by
hand-auditing (`docs/reference/MODEL_ACCESS_INVENTORY.md`, "The eight uncoordinated budgets"),
and finding them by hand is precisely what does not scale — the audit that found them was
itself 24% wrong about its own line numbers four commits later.

This is the mechanism `docs/plans/LOCAL_MODEL_LEDGER_PLAN.md` L2 asks for, landed at the stage
the code is actually at. It does NOT claim there is one reader; there are several, and L3 is
what collapses them. What it claims is that the SET IS THIS ONE, so adding a seventh budget
takes a deliberate edit to this file rather than a reviewer happening to notice — the failure
mode `test_llm_load_guard_chokepoint.py` exists for, in its own words: "a reviewer noticing is
what already failed three times."
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "jbrain"

# module path -> the memory readings it is allowed to take, and WHY that one is legitimate.
# Adding a row here is a decision about the box's safety, not bookkeeping: read the plan's
# "Why the obvious fix is wrong" before you do it.
_APPROVED: dict[str, set[str]] = {
    # The pre-flight (host + device) and the runaway watchdog. The host term here is the one
    # that can actually fire on this box's `amdgpu.gttsize` configuration.
    "llm/gpu_guard.py": {"read_memory_gb", "sample"},
    # The eviction budget (`_plan`), the device pre-flight's baseline, and the restore's census.
    "llm/residency.py": {"read_memory_gb", "sample"},
    # The load's own device baseline, kept for the predicted-vs-measured comparison.
    "llm/local_gateway.py": {"sample"},
    # DISPLAY ONLY — the settings screen's memory meter. It decides nothing, and it is in this
    # list rather than exempt from it because "it only displays" is how a reader stops being
    # only a display.
    "api/llm_settings.py": {"read_memory_gb"},
}

_WATCHED = {"read_memory_gb", "sample"}


def _readings() -> dict[str, set[str]]:
    """Every call to a watched memory reader, by module path relative to `src/jbrain`."""
    found: dict[str, set[str]] = {}
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in _WATCHED:
                continue
            # The definitions themselves are not readings; only calls reach here, so a
            # `def sample` or `def read_memory_gb` never counts.
            found.setdefault(str(path.relative_to(_SRC)), set()).add(name)
    return found


def test_only_the_approved_modules_read_the_box_s_memory() -> None:
    actual = _readings()
    unexpected = {
        module: sorted(names - _APPROVED.get(module, set()))
        for module, names in actual.items()
        if names - _APPROVED.get(module, set())
    }
    assert not unexpected, (
        f"a new memory reader appeared: {unexpected}. Every co-resident-model failure this box "
        "has had came from a second place reading memory and disagreeing with the first. If "
        "this reading really is needed, add it to _APPROVED with the reason — and read "
        "docs/plans/LOCAL_MODEL_LEDGER_PLAN.md first, because the answer is usually the ledger."
    )


def test_the_inventory_has_no_stale_entries() -> None:
    """The other direction, and the one an allowlist normally rots in. A module that stopped
    reading memory must leave this list, or L3's "collapse the duplicate budgets" ends with a
    file that still claims six readers while the code has one."""
    actual = _readings()
    stale = {
        module: sorted(names - actual.get(module, set()))
        for module, names in _APPROVED.items()
        if names - actual.get(module, set())
    }
    assert not stale, (
        f"_APPROVED lists readings that no longer exist: {stale}. Remove them — an allowlist "
        "nobody prunes stops being evidence of anything."
    )
