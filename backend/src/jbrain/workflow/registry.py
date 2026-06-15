"""The action registry: the six job handlers described as data, bound to the
handler that runs each, validated at boot (E3, docs/WORKFLOW_ENGINE_PLAN.md §2).

An `action` *names* an existing registered handler — the engine cannot invent a
handler or call arbitrary code. Pipeline/trigger rows reference actions by
name+version only; the registry is the single place that maps a name to the
callable behind it. Like the `.tool`/schema registries it validates at startup
(an action without a handler, or a handler without an action, fails fast), so a
misconfigured action set never serves a single job.

The specs here are the in-code source of truth; the `app.actions` table (migration
0035) is their reference projection for the engine's data layer. Keeping the two in
lockstep is the boot validation's job: `validate()` requires an exact name match
between the registered specs and the live handler dispatch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

# A handler runs one job: it receives the job payload (row IDs only — never note
# content) and performs the work. The worker, not the registry, invokes it, so the
# registry stores the binding opaquely.
Handler = Callable[[dict[str, Any]], Awaitable[None]]

CostClass = Literal["cheap", "standard", "expensive"]
"""How consequential an action's spend is — mirrors the `.tool` cost classes so the
scheduler/budget (E5) can meter expensive actions without inspecting handler code."""


class ActionRegistryError(ValueError):
    """An action lacks a handler, a handler lacks an action, or an action name is
    duplicated — raised at startup so a misconfigured action set never dispatches."""


@dataclass(frozen=True)
class ActionSpec:
    """One job handler described as data (the `app.actions` row shape).

    `handler` is the dispatch key the worker binds to a callable — the indirection
    that lets a pipeline reference an action by name without the action embedding
    code. `mutating` and `cost_class` let the engine reason about an action's blast
    radius and spend without running it; `dedup_key_expr` is an optional hint naming
    the payload field that makes a job idempotent (the existing `has_active` dedup,
    E4) — advisory metadata for now, the handler still enforces write-once.
    """

    name: str
    version: int
    handler: str
    params_schema: dict[str, Any] = field(default_factory=dict)
    # Reference data: whether the action may run without a domain scope (the
    # cross-domain ingest/integration pipelines), mirroring the `actions` column.
    domain_optional: bool = True
    mutating: bool = True
    cost_class: CostClass = "standard"
    dedup_key_expr: str | None = None


class ActionRegistry:
    """An immutable set of registered actions, keyed by name.

    Each spec carries its `handler` dispatch key; `validate` proves every key binds
    to a real handler before the worker serves a job, and `dispatch_table` projects
    the registry back into the `{kind: handler}` map the worker loop consumes.
    """

    def __init__(self, specs: Sequence[ActionSpec]):
        by_name: dict[str, ActionSpec] = {}
        for spec in specs:
            if spec.name in by_name:
                raise ActionRegistryError(f"duplicate action name: {spec.name!r}")
            by_name[spec.name] = spec
        self._by_name = by_name

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)

    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)

    def get(self, name: str) -> ActionSpec:
        try:
            return self._by_name[name]
        except KeyError:
            raise ActionRegistryError(f"unknown action: {name!r}") from None

    def validate(self, handlers: Mapping[str, Handler]) -> None:
        """Prove the registry and the handler dispatch match exactly.

        Fails (ActionRegistryError) if an action's handler key has no callable or a
        handler has no action — the two must agree, so an action can never be
        advertised without code to run it, nor a handler shipped the engine cannot
        reach. This is the boot gate that turns a runtime "no handler for kind"
        failure into a startup failure (W0.1).
        """
        action_keys = {spec.handler for spec in self._by_name.values()}
        handler_keys = set(handlers)
        if missing_handlers := action_keys - handler_keys:
            raise ActionRegistryError(f"actions without handlers: {sorted(missing_handlers)}")
        if missing_actions := handler_keys - action_keys:
            raise ActionRegistryError(f"handlers without actions: {sorted(missing_actions)}")

    def dispatch_table(self, handlers: Mapping[str, Handler]) -> dict[str, Handler]:
        """The `{job kind: handler}` map the worker loop consumes, built only after
        `validate` passes. The job `kind` is the action's `handler` key, so behavior
        for known kinds is identical to the previous hardcoded dict."""
        self.validate(handlers)
        return {spec.handler: handlers[spec.handler] for spec in self._by_name.values()}


# The six handlers shipped through Phase 4, described as data. The `handler` key is
# the existing `app.jobs.kind` so the dispatch table the worker builds is identical.
# `mutating`/`cost_class` are conservative: every one writes (notes, embeddings,
# facts, the predicate index), and the LLM-backed analysis/OCR actions are costed
# above the pure-DB sweeps. These are mirrored as seed rows in migration 0035.
ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        name="ingest_note",
        version=1,
        handler="ingest_note",
        domain_optional=True,
        mutating=True,
        cost_class="standard",
        dedup_key_expr="note_id",
    ),
    ActionSpec(
        name="embed_note",
        version=1,
        handler="embed_note",
        domain_optional=True,
        mutating=True,
        cost_class="standard",
        dedup_key_expr="note_id",
    ),
    ActionSpec(
        name="integrate_note",
        version=1,
        handler="integrate_note",
        domain_optional=True,
        mutating=True,
        cost_class="expensive",
        dedup_key_expr="note_id",
    ),
    ActionSpec(
        name="ocr_attachment",
        version=1,
        handler="ocr_attachment",
        domain_optional=True,
        mutating=True,
        cost_class="expensive",
        dedup_key_expr="attachment_id",
    ),
    ActionSpec(
        name="consolidate_predicates",
        version=1,
        handler="consolidate_predicates",
        domain_optional=True,
        mutating=True,
        cost_class="standard",
        dedup_key_expr=None,
    ),
    ActionSpec(
        name="sync_predicates",
        version=1,
        handler="sync_predicates",
        domain_optional=True,
        mutating=True,
        cost_class="standard",
        dedup_key_expr=None,
    ),
)


def build_registry(specs: Sequence[ActionSpec] = ACTION_SPECS) -> ActionRegistry:
    """The in-code action registry (the shipped six by default)."""
    return ActionRegistry(specs)
