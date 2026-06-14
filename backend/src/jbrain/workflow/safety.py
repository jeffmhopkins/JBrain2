"""The "IDs not payloads" guard — DBOS adoption condition #1.

DBOS persists every workflow/step argument and return value into its own system
schema, which lives outside our Alembic-managed, RLS-policed tables. Our security
model forbids firewalled content (health / finance / location note text) from
landing in an unprotected store. The discipline that keeps non-negotiable #3 intact
is therefore: **workflows and steps pass row IDs and handles, never the content
behind them** — a step re-fetches through an RLS-scoped session when it needs the
body.

This module makes that discipline checkable. `assert_reference_shaped` rejects a
payload that carries content rather than references, so a step that accidentally
returns a note body fails loudly in tests instead of silently leaking it into the
`dbos` schema. The string-length cutoff is a deliberate heuristic seed, not a
proof: it catches the common leak (a note/chunk/LLM body) while allowing IDs,
enum values, and short identifiers. It is the executable form of the guard test the
research calls for, to be tightened as real blocks are written.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

# A reference is short and single-line: UUIDs (36), slugs, enum values, short
# names. Note/chunk/LLM bodies blow past this — which is exactly the leak we want
# to catch. Tune as real blocks land.
MAX_REFERENCE_STR = 256


class PayloadLeakError(AssertionError):
    """A workflow/step payload carried content where only a reference belongs —
    i.e. it would serialize firewalled data into DBOS's system schema."""


def is_reference_shaped(value: Any) -> bool:
    """True when `value` is built only of reference-shaped leaves: None, bools,
    numbers, UUIDs, short single-line strings, and containers thereof."""
    if value is None or isinstance(value, (bool, int, float, uuid.UUID)):
        return True
    if isinstance(value, str):
        return len(value) <= MAX_REFERENCE_STR and "\n" not in value
    if isinstance(value, Mapping):
        return all(
            is_reference_shaped(k) and is_reference_shaped(v) for k, v in value.items()
        )
    # str is a Sequence; handled above, so any Sequence here is list/tuple-like.
    if isinstance(value, Sequence):
        return all(is_reference_shaped(item) for item in value)
    return False


def assert_reference_shaped(value: Any, *, where: str = "workflow payload") -> None:
    """Raise `PayloadLeakError` if `value` is not reference-shaped. Call on step
    inputs/outputs in tests (and, cheaply, at runtime) to enforce condition #1."""
    if not is_reference_shaped(value):
        raise PayloadLeakError(
            f"{where} is not reference-shaped — pass IDs/handles, not content, so "
            f"nothing firewalled is serialized into the DBOS system schema"
        )
