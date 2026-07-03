"""The egress chokepoint: a Connector abstraction and the egress guard
(docs/reference/ASSISTANT.md "External connectors", invariant #9).

No tool ever makes a raw HTTP request. A Connector is a named, owner-configured
upstream with a **pinned base URL** (config, never model-supplied), a **typed
request schema**, a response parser, a cache policy, a domain tag, and a consent
flag. The **egress guard** builds the outbound request from typed params only —
the model fills declared slots, never a URL, never free-form passthrough — and
rejects anything beyond the declared shape, so the conversation context (and the
owner data in it) cannot be stuffed into a query string. The resulting
EgressRequest is the exact outbound payload the owner approves before it leaves the
box.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ParamKind = type  # str | int | float — the declared slot type


@dataclass(frozen=True)
class ParamSpec:
    """One declared input slot: its name, its type, and whether it is required.
    A param the model sends that is not declared here is rejected (egress guard)."""

    name: str
    kind: ParamKind
    required: bool = True


# A parser turns the upstream's parsed JSON into a concise text summary the agent
# reads as DATA (wrapped in the I-1 boundary by the caller).
ResponseParser = Callable[[Any], str]


@dataclass(frozen=True)
class Connector:
    """A named, pinned, typed upstream. `base_url` comes from config and is never
    model-supplied; `params` is the entire allowed input shape."""

    name: str
    base_url: str
    path: str
    domain: str
    params: tuple[ParamSpec, ...]
    parse: ResponseParser
    consent_required: bool = True
    ttl_seconds: int = 86400


@dataclass(frozen=True)
class EgressRequest:
    """The exact outbound payload — built from typed slots only. This is what the
    owner approves before it leaves the box (the egress Proposal's preview)."""

    connector: str
    method: str
    url: str
    query: dict[str, str]

    @property
    def input_hash(self) -> str:
        """A stable hash of the normalized input — the cache key and the log's
        payload-free fingerprint."""
        canonical = json.dumps({"q": self.query}, sort_keys=True)
        return hashlib.sha256(f"{self.connector}\n{canonical}".encode()).hexdigest()


class EgressGuardError(ValueError):
    """A request was rejected before any network call — an undeclared param, a
    missing required slot, or a value that doesn't match its declared type."""


def build_egress(connector: Connector, raw_params: Mapping[str, Any]) -> EgressRequest:
    """Build the outbound request from typed params only, or raise. The load-
    bearing rule (#9): a param key not declared on the connector is rejected, so
    nothing from the conversation context can ride along into the query string."""
    allowed = {p.name: p for p in connector.params}
    undeclared = sorted(set(raw_params) - set(allowed))
    if undeclared:
        raise EgressGuardError(f"{connector.name}: rejected undeclared params: {undeclared}")
    query: dict[str, str] = {}
    for spec in connector.params:
        if spec.name not in raw_params:
            if spec.required:
                raise EgressGuardError(f"{connector.name}: missing required param: {spec.name}")
            continue
        value = raw_params[spec.name]
        try:
            coerced = spec.kind(value)
        except (TypeError, ValueError) as exc:
            raise EgressGuardError(
                f"{connector.name}: param {spec.name!r} is not a {spec.kind.__name__}"
            ) from exc
        query[spec.name] = str(coerced)
    url = connector.base_url.rstrip("/") + connector.path
    return EgressRequest(connector=connector.name, method="GET", url=url, query=query)


class ConnectorRegistry:
    """The fixed allowlist of connectors, each disable-able by the owner. Unknown
    connector names are rejected — there is no arbitrary-fetch escape hatch."""

    def __init__(self, connectors: Sequence[Connector], disabled: frozenset[str] = frozenset()):
        self._by_name = {c.name: c for c in connectors}
        self._disabled = disabled

    def get(self, name: str) -> Connector:
        if name in self._disabled:
            raise EgressGuardError(f"connector {name!r} is disabled")
        try:
            return self._by_name[name]
        except KeyError:
            raise EgressGuardError(f"unknown connector: {name!r}") from None

    def names(self) -> set[str]:
        return set(self._by_name) - self._disabled
