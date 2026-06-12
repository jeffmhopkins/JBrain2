"""Write-time memory-domain classifier — owned by the memory layer, fail-closed,
and deterministic (no per-write LLM call). It decides the domain stamp every
memory write carries, and getting it wrong must fail *closed*: over-restricting
hides a memory from a session that could have read it (annoying); under-
restricting leaks it to one that should not (a firewall breach). Every default
here leans toward the restrictive side (docs/ASSISTANT.md "Domain classification",
invariants #3/#4; the asymmetric rule is docs/ANALYSIS.md).

This module is pure policy — the RLS columns it produces are what Postgres
enforces. It is immutable to self-edit (invariant #12).
"""

from collections.abc import Iterable, Sequence

# Sensitivity ranking for the asymmetric rule: misclassifying a behavioral memory
# INTO a more-sensitive domain is cheap; OUT of it is a leak — so when a write
# touches a sensitive domain, the stamp defaults into the most sensitive one.
# `general` is the only non-firewalled (non-sensitive) domain.
_SENSITIVITY: dict[str, int] = {"general": 0, "location": 1, "finance": 2, "health": 3}


def _sensitivity(domain: str) -> int:
    # An unknown domain is treated as maximally sensitive — fail closed.
    return _SENSITIVITY.get(domain, max(_SENSITIVITY.values()) + 1)


def episodic_scopes(touched: Iterable[str], session_scopes: Sequence[str]) -> tuple[str, ...]:
    """The domains an episodic trace is stamped with: every scope the turn's tools
    actually read, bounded by the session's selected scopes (its upper bound).

    Fail-closed: a trace is readable later only by a session holding *all* of
    these (the agent_episodes RLS), so an over-stamp merely hides it while an
    under-stamp would leak it. When nothing domain-specific was observed we stamp
    the full session scope set rather than mint a bare `general` row — invariant
    #4 forbids decomposing a multi-scope turn into `general`.
    """
    bound = set(session_scopes)
    observed = {d for d in touched if d in bound}
    return tuple(sorted(observed or bound))


def behavioral_domain(touched: Iterable[str], *, owner_confirmed: bool) -> str | None:
    """The single domain stamp for a behavioral/self-semantic memory write, or
    `None` when the write is rejected.

    Behavioral memory is **owner-confirmed-write only** (invariant #3): a
    non-confirmed write returns `None` and must not be persisted. A confirmed
    write defaults INTO the most-sensitive domain it touched (the asymmetric
    rule); `general` only when it provably touched nothing sensitive.
    """
    if not owner_confirmed:
        return None
    touched = list(touched)
    if not touched:
        return "general"
    return max(touched, key=_sensitivity)


def behavioral_needs_review(touched: Iterable[str]) -> bool:
    """A behavioral write that touches more than one *sensitive* domain is
    ambiguous and consequential: which firewall should own it is not obvious, so
    it routes to the review inbox instead of being auto-stamped (the classifier
    refuses to guess across firewalls). Single-domain or general-only writes are
    unambiguous."""
    sensitive = {d for d in touched if _sensitivity(d) > 0}
    return len(sensitive) > 1
