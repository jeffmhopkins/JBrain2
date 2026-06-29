"""A small in-memory feed of debug-console activity, so the web console can show
commands **as they happen** — including ones an external assistant runs over curl,
not just the ones typed in that browser tab.

Every `/api/debug/*` request is recorded here by a middleware (main.py) with its
method, path, status, a derived `kind`, and a short `detail` — the SQL text, the
prompt, the routing change — that the handler stashes on `request.state` so the
owner can see WHAT ran, not just the route. The detail is truncated; this surface
is owner-token-gated, and the owner already has full read, so showing their own
commands back to them is intentional. The ring is process-local and best-effort —
a live view, not an audit log.
"""

import datetime as dt
from collections import deque
from typing import Any

_RING = 200
_DETAIL_MAX = 300


def classify(method: str, path: str) -> str:
    """A short command label for a debug route, for the console's type badge."""
    tail = path.removeprefix("/api/debug/")
    if tail in ("complete", "complete-async"):
        return "complete"
    if tail.startswith("logs/"):
        return "logs"
    if tail.startswith("llm/local-models/"):
        return "unload" if tail.endswith("/unload") else "load"
    if tail == "llm":
        return "switch" if method == "PUT" else "routing"
    if tail in {"whoami", "complete", "sql", "host", "suspend-self", "revoke-self"}:
        return {"suspend-self": "suspend", "revoke-self": "revoke"}.get(tail, tail)
    return tail or "debug"


class DebugActivity:
    def __init__(self, maxlen: int = _RING) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0

    def record(self, *, method: str, path: str, status: int, client: str, detail: str = "") -> None:
        clipped = detail if len(detail) <= _DETAIL_MAX else detail[:_DETAIL_MAX] + "…"
        self._seq += 1
        self._events.append(
            {
                "seq": self._seq,
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "method": method,
                "path": path,
                "status": status,
                "kind": classify(method, path),
                "detail": clipped,
                "client": client,
            }
        )

    def snapshot(self, after: int | None = None, limit: int = 50) -> dict[str, Any]:
        """Events newer than `after` (or the most recent `limit` when omitted), plus
        the current high-water `seq` so the caller can poll incrementally."""
        events = list(self._events)
        events = [e for e in events if e["seq"] > after] if after is not None else events[-limit:]
        return {"events": events, "last": self._seq}
