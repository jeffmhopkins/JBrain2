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

    def schemas_for(self, scopes: Collection[str]) -> list[LlmTool]:
        """The adapter tool definitions a session holding `scopes` may see —
        visibility only; RLS at the DB layer is the boundary. Stable order so a
        prompt's tool list does not churn between turns."""
        return [
            tool.as_llm_tool()
            for name in sorted(self._by_name)
            if _visible((tool := self._by_name[name]).spec.domains, scopes)
        ]


def load_registry(tools_dir: Path, handlers: Mapping[str, ToolHandler]) -> ToolRegistry:
    """Load every `.tool` sidecar under `tools_dir` and bind it to its handler.

    Fails (ToolRegistryError) if a sidecar has no handler or a handler has no
    sidecar — the two must match exactly, so a tool can never be advertised to the
    model without code to run it, nor a handler shipped the model cannot reach.
    """
    loaded = [load_tool(path) for path in sorted(tools_dir.glob("*.tool"))]
    sidecar_names = {tf.spec.name for tf in loaded}
    handler_names = set(handlers)
    if missing_handlers := sidecar_names - handler_names:
        raise ToolRegistryError(f"sidecars without handlers: {sorted(missing_handlers)}")
    if missing_sidecars := handler_names - sidecar_names:
        raise ToolRegistryError(f"handlers without sidecars: {sorted(missing_sidecars)}")
    # Duplicate sidecar names (same name in two files) surface in ToolRegistry.
    return ToolRegistry(
        [RegisteredTool(toolfile=tf, handler=handlers[tf.spec.name]) for tf in loaded]
    )
