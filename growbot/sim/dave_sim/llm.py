"""Dave's model, for a simulated run: the real one on the box, or a script.

Live runs send the page's own chat request, untouched, to the box's creature lane through
the owner debug API (`POST /api/debug/growbot/replay`, docs/runbooks/DEBUG_ACCESS.md): the
same model, slot and serving path as the robot's phone. The box refuses (409) while the real
creature has spoken in the last two minutes, so a run never takes the slot from Jeff's: a
turn that stays refused reaches the page as a failed call, as it would on the phone.
Scripted runs answer from a list, for tests that must not depend on a model.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{40,}={0,2}")


def debug_token(path: str | None = None) -> tuple[str, str]:
    """The box's base URL and key from an owner debug token: `$JBRAIN_DEBUG_TOKEN`, or the
    first token-shaped string in the file at `path` (or `$JBRAIN_DEBUG_TOKEN_FILE`)."""
    raw = os.environ.get("JBRAIN_DEBUG_TOKEN", "")
    src = path or os.environ.get("JBRAIN_DEBUG_TOKEN_FILE", "")
    if not raw and src:
        found = TOKEN_RE.findall(Path(src).read_text(errors="replace"))
        raw = found[0] if found else ""
    if not raw:
        raise RuntimeError("no debug token: set JBRAIN_DEBUG_TOKEN or JBRAIN_DEBUG_TOKEN_FILE")
    d = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    if not str(d["u"]).startswith("https://"):
        raise RuntimeError("the debug token's box URL is not https")
    return d["u"].rstrip("/"), d["k"]


class BoxApi:
    """The owner debug API: persona reads and the creature lane. Nothing here changes the
    persona, though the box logs each replayed turn (soul included) as a `replay` call row."""

    def __init__(self, base: str, key: str) -> None:
        self.base, self.key = base, key

    def call(self, method: str, path: str, body: Any = None, timeout: float = 120.0) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        # Unredirected: a redirect anywhere must never carry the owner key with it.
        req.add_unredirected_header("Authorization", f"Bearer {self.key}")
        # The tunnel's edge turns away Python's default agent string.
        req.add_header("User-Agent", "dave-sim/1")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def persona(self, name: str) -> dict[str, Any]:
        """A persona by name, with its whole soul (read only)."""
        got = self.call("GET", "/api/debug/growbot/personas")
        rows = got.get("personas", []) if isinstance(got, dict) else got
        hit = [p for p in rows if str(p.get("name", "")).lower() == name.lower()]
        if not hit:
            raise RuntimeError(f"no persona named {name!r} on the box")
        return self.call("GET", f"/api/debug/growbot/personas/{hit[0]['id']}")


# The page gives up on a model call after 30 s, so a busy lane is waited out inside that
# and then answered as the box would (an error), never after the page has moved on.
def live_model(api: BoxApi, busy_wait_s: float = 5.0, tries: int = 4):
    """The box's creature lane as the run's model."""

    async def ask(req: dict[str, Any]) -> str:
        if int(req.get("max_tokens") or 0) <= 2:
            # The page's cache seal after each turn (one token, its answer unused): it only
            # warms the real phone's slot, so a run doesn't spend the box on it.
            return ""
        body = {**req, "stream": False}
        for attempt in range(tries):
            try:
                got = await asyncio.to_thread(api.call, "POST", "/api/debug/growbot/replay", body)
            except urllib.error.HTTPError as e:
                if e.code == 409 and attempt < tries - 1:
                    # The real creature is talking: wait for its quiet, never take its slot.
                    await asyncio.sleep(busy_wait_s)
                    continue
                raise
            return got["choices"][0]["message"]["content"]
        raise RuntimeError("the box's creature lane stayed busy")

    return ask


def scripted_model(replies: Iterable[dict[str, Any] | str] | Callable[[dict[str, Any]], Any]):
    """Answers from a list (each a reply object or its JSON), in order, then `{"say": ""}`;
    or from a function of the request."""
    if callable(replies):
        fn = replies

        async def ask_fn(req: dict[str, Any]) -> str:
            out = fn(req)
            return out if isinstance(out, str) else json.dumps(out)

        return ask_fn
    queue = list(replies)

    async def ask(req: dict[str, Any]) -> str:
        out = queue.pop(0) if queue else {"say": ""}
        return out if isinstance(out, str) else json.dumps(out)

    return ask
