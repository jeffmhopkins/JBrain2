"""The tool registry: discover `.tool` sidecars, bind each to its handler, and
expose the in-scope tool schemas to the agent loop.

The registry is the single place that knows which tools exist. It validates at
startup (a sidecar without a handler, a handler without a sidecar, or a duplicate
name all fail fast) and answers two questions the loop asks every turn: *which
tools may this session see?* (`schemas_for`, filtered by domain scope — visibility)
and *what runs for this tool call?* (`get`). RLS at the DB layer remains the
actual firewall; visibility is convenience.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jbrain.agent.contracts import PermissionClass, ToolSpec
from jbrain.agent.toolfile import ToolFile, load_tool
from jbrain.llm import LlmTool

# A handler runs one tool call: it receives the parsed arguments and a context the
# loop supplies (the RLS-scoped session, the principal). The registry stores it
# opaquely — the loop, not the registry, invokes it — so the precise context type
# is the loop's concern (P4.4).
ToolHandler = Callable[..., Awaitable[Any]]

# Tools that must NEVER be absorbed by the `allow=None` knowledge-agent wildcard,
# independent of their permission class — the spawn primitive is opt-in per agent
# (jerv + the research/review children) and must never fall to the curator
# (docs/archive/SUBAGENT_SPAWNING_PLAN.md, review B3). The name is the single source of
# truth; `agents.SPAWN_TOOL` matches it (asserted in tests, kept here to avoid an
# agents→toolregistry import cycle).
NEVER_DEFAULT: frozenset[str] = frozenset({"spawn_subagent", "deep_research"})


class ToolRegistryError(ValueError):
    """A sidecar lacks a handler, a handler lacks a sidecar, or a tool name is
    duplicated — raised at startup so a misconfigured tool set never serves."""


@dataclass(frozen=True)
class RegisteredTool:
    """A sidecar bound to the handler that runs it."""

    toolfile: ToolFile
    handler: ToolHandler

    @property
    def spec(self) -> ToolSpec:
        return self.toolfile.spec

    @property
    def name(self) -> str:
        return self.toolfile.spec.name

    @property
    def permission(self) -> PermissionClass:
        return self.toolfile.spec.permission

    def as_llm_tool(self) -> LlmTool:
        """The adapter-facing definition: the model reads the description and the
        arguments schema."""
        return LlmTool(
            name=self.spec.name,
            description=self.toolfile.description,
            input_schema=self.spec.params,
        )


def _visible(domains: Sequence[str], scopes: Collection[str]) -> bool:
    """A tool with no declared domains is visible to any session; otherwise the
    session must hold at least one of the tool's domains."""
    return not domains or any(d in scopes for d in domains)


class ToolRegistry:
    """An immutable set of registered tools, queried by the loop."""

    def __init__(self, tools: Sequence[RegisteredTool]):
        by_name: dict[str, RegisteredTool] = {}
        for tool in tools:
            if tool.name in by_name:
                raise ToolRegistryError(f"duplicate tool name: {tool.name!r}")
            by_name[tool.name] = tool
        self._by_name = by_name

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __len__(self) -> int:
        return len(self._by_name)

    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._by_name[name]
        except KeyError:
            raise ToolRegistryError(f"unknown tool: {name!r}") from None

    def _admits(
        self, tool: RegisteredTool, scopes: Collection[str], allow: Collection[str] | None
    ) -> bool:
        """Whether a session holding `scopes` under the agent's `allow` list may use
        this tool. `allow=None` is the default knowledge agent (every in-scope tool
        EXCEPT the opt-in `web` class); a collection names the exact tools the agent
        may call (an empty collection = none). A `web` tool is admitted only when
        explicitly allowlisted, so the Full Brain `curator` never gains arbitrary
        internet access."""
        if allow is not None and tool.name not in allow:
            return False
        # The web class is opt-in: never admitted to the default knowledge agent.
        if allow is None and tool.spec.permission == "web":
            return False
        # Never-default tools (spawn_subagent) are excluded from the `allow=None`
        # wildcard even were they not web-classed, so curator's tools=None can never
        # absorb the spawn primitive (docs/archive/SUBAGENT_SPAWNING_PLAN.md, review B3).
        if allow is None and tool.name in NEVER_DEFAULT:
            return False
        return _visible(tool.spec.domains, scopes)

    def schemas_for(
        self, scopes: Collection[str], allow: Collection[str] | None = None
    ) -> list[LlmTool]:
        """The adapter tool definitions a session may see — visibility only; RLS at
        the DB layer is the boundary, and `allowed_names` is the dispatch-time gate.
        Stable order so a prompt's tool list does not churn between turns."""
        return [
            self._by_name[name].as_llm_tool()
            for name in sorted(self._by_name)
            if self._admits(self._by_name[name], scopes, allow)
        ]

    def allowed_names(
        self, scopes: Collection[str], allow: Collection[str] | None = None
    ) -> frozenset[str]:
        """The names a session may actually call — the dispatch-time enforcement of
        the same gate `schemas_for` applies to visibility. The loop checks a tool
        call against THIS, so a model that names a tool it was never offered (a slip
        or an injection) is refused, not run — the allowlist is a boundary, not a
        hint (closes the `curator`-can't-reach-`web` invariant structurally)."""
        return frozenset(
            name for name, tool in self._by_name.items() if self._admits(tool, scopes, allow)
        )


def load_registry(
    tools_dir: Path,
    handlers: Mapping[str, ToolHandler],
    *,
    optional: Collection[str] = (),
) -> ToolRegistry:
    """Load every `.tool` sidecar under `tools_dir` and bind it to its handler.

    Fails (ToolRegistryError) if a sidecar has no handler or a handler has no
    sidecar — the two must match exactly, so a tool can never be advertised to the
    model without code to run it, nor a handler shipped the model cannot reach.

    `optional` names sidecars that may be absent when their feature is unconfigured:
    such a sidecar without a handler is SKIPPED (not loaded) rather than failing,
    so a feature like image generation can be gated off entirely (graceful degrade,
    docs/archive/IMAGE_GEN_PLAN.md). The strict pairing still holds for every other sidecar,
    and a handler still always needs a sidecar.
    """
    loaded = [load_tool(path) for path in sorted(tools_dir.glob("*.tool"))]
    handler_names = set(handlers)
    optional_set = set(optional)
    # An optional sidecar with no handler is dropped; everything else must pair.
    kept = [
        tf for tf in loaded if tf.spec.name in handler_names or tf.spec.name not in optional_set
    ]
    sidecar_names = {tf.spec.name for tf in kept}
    if missing_handlers := sidecar_names - handler_names:
        raise ToolRegistryError(f"sidecars without handlers: {sorted(missing_handlers)}")
    if missing_sidecars := handler_names - sidecar_names:
        raise ToolRegistryError(f"handlers without sidecars: {sorted(missing_sidecars)}")
    # Duplicate sidecar names (same name in two files) surface in ToolRegistry.
    return ToolRegistry(
        [RegisteredTool(toolfile=tf, handler=handlers[tf.spec.name]) for tf in kept]
    )
