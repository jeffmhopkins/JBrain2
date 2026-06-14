"""The block registry: one library of named, versioned units of work.

A *block* is the engine's atom — a Python callable or an LLM call — that both
execution surfaces share: the agent invokes blocks live (one per turn), and a
pipeline sequences them under a trigger. Keeping a single registry is the
highest-leverage simplification from the workflow-engine research: it is why
"reuse" falls out for free instead of being a feature.

This is intentionally independent of `jbrain.agent` (the `.tool` sidecar registry)
during the spike. The two are designed to converge — a `.tool` is a block whose
metadata happens to render to a provider tool-schema — but unifying them touches
the agent loop, so it is sequenced after the engine exists. The fields here mirror
the agent's `ToolSpec` (`version`, `domains`, `mutating`, `side_effecting`) so that
convergence is a merge, not a rewrite.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

BlockKind = Literal["python", "llm"]


class BlockError(ValueError):
    """A block definition or invocation that is wrong at registration/validation
    time — raised eagerly so a bad pipeline fails before a run starts, not at 3am
    mid-execution (the diagnosability goal from the research)."""


@dataclass(frozen=True)
class BlockSpec:
    """A block's declaration. `params` is a Pydantic model so a pipeline's bound
    arguments validate up front and the schema is introspectable for the UI."""

    name: str
    version: int
    params: type[BaseModel]
    kind: BlockKind
    domains: tuple[str, ...]
    description: str
    mutating: bool = False
    side_effecting: bool = False

    def __post_init__(self) -> None:
        if not self.name or " " in self.name:
            raise BlockError(f"block name must be a non-empty identifier: {self.name!r}")
        if self.version < 1:
            raise BlockError(f"block {self.name!r} version must be >= 1")
        if not self.domains:
            raise BlockError(f"block {self.name!r} must declare at least one domain")
        if not issubclass(self.params, BaseModel):
            raise BlockError(f"block {self.name!r} params must be a pydantic model")


@dataclass
class BlockRegistry:
    """Discover/bind blocks and answer the two questions every caller asks: *what
    blocks exist (filtered by domain scope)?* and *bind this name to its callable
    and validate its params.*"""

    _blocks: dict[str, tuple[BlockSpec, Callable[..., Any]]] = field(default_factory=dict)

    def register(self, spec: BlockSpec, fn: Callable[..., Any]) -> None:
        if spec.name in self._blocks:
            raise BlockError(f"block {spec.name!r} already registered")
        self._blocks[spec.name] = (spec, fn)

    def spec(self, name: str) -> BlockSpec:
        return self._require(name)[0]

    def handler(self, name: str) -> Callable[..., Any]:
        return self._require(name)[1]

    def validate_params(self, name: str, raw: dict[str, Any]) -> BaseModel:
        """Parse a pipeline's bound params against the block's model. A validation
        failure here is a definition error, surfaced as `BlockError`."""
        spec = self._require(name)[0]
        try:
            return spec.params.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError → engine-level error
            raise BlockError(f"block {name!r} got invalid params: {exc}") from exc

    def names(self, *, domain_scope: tuple[str, ...] | None = None) -> list[str]:
        """All block names, or only those visible to a domain scope — the same
        visibility filter the agent's tool registry applies per session."""
        if domain_scope is None:
            return sorted(self._blocks)
        scope = set(domain_scope)
        return sorted(
            name
            for name, (spec, _) in self._blocks.items()
            if scope.intersection(spec.domains)
        )

    def _require(self, name: str) -> tuple[BlockSpec, Callable[..., Any]]:
        if name not in self._blocks:
            raise BlockError(f"unknown block {name!r}")
        return self._blocks[name]

    def __contains__(self, name: object) -> bool:
        return name in self._blocks

    def __len__(self) -> int:
        return len(self._blocks)


def block(
    registry: BlockRegistry,
    *,
    name: str,
    version: int,
    params: type[BaseModel],
    kind: BlockKind = "python",
    domains: tuple[str, ...] = ("general",),
    description: str = "",
    mutating: bool = False,
    side_effecting: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: register a callable as a block in `registry`. Returns the
    callable unchanged so it stays directly callable (and DBOS can wrap it as a
    step)."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        registry.register(
            BlockSpec(
                name=name,
                version=version,
                params=params,
                kind=kind,
                domains=domains,
                description=description or (fn.__doc__ or "").strip(),
                mutating=mutating,
                side_effecting=side_effecting,
            ),
            fn,
        )
        return fn

    return decorate
