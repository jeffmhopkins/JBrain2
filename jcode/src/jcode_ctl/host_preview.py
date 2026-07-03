"""Host-served per-session preview allocation (Wave P1 of
``docs/archive/JCODE_PREVIEW_HOST_PLAN.md``).

Each session gets a dev **port** from a bounded pool and a stable, unguessable
**hostname** under the box's OWN named tunnel — no per-session TryCloudflare
quick-tunnel, so no rate limit and no public-resolver DNS dependence. This module
owns only the allocation + lookup; the api↔jcode reverse-proxy that fronts it is
Wave P2 and the edge/DNS wiring is Wave P3. Pure in-memory — no subprocess, no
network. This is the sole preview path since the Wave P5b cutover retired the
per-session ``cloudflared`` quick-tunnel adapter; it fail-closes (``enabled`` is
False) when no base host is configured.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from jcode_ctl.preview import PreviewError

# Share the preview logger so host + tunnel paths read as one feed in the debug
# console (verbose when debug access is on — Wave P0).
_log = logging.getLogger("jcode_ctl.preview")


@dataclass(frozen=True)
class Allocation:
    """A session's reserved preview port, its unguessable slug, and the resulting
    public URL. Stable for the session's life so the dev server's ``$PORT`` never
    shifts under a restart."""

    port: int
    slug: str
    url: str


class HostPreviewManager:
    """Owns the per-session port + hostname reservations for host-mode preview.

    Reservations live from ``ensure`` (first needed — when the session's shell is
    created, Wave P2) to ``release`` (delete / reap). Pausing a session does NOT
    release: the port is just a number and the slug an opaque token, and a paused
    session is made unreachable at the proxy (Wave P2), not by tearing the
    reservation down — so a restart resumes on the same port and URL.
    """

    def __init__(
        self, *, base_host: str, port_low: int = 5173, port_high: int = 5199
    ) -> None:
        self._base_host = base_host.strip().strip(".")
        self._low = port_low
        self._high = port_high
        self._by_sid: dict[str, Allocation] = {}
        self._by_slug: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        # Fail-closed: with no base host there is nowhere to serve, so the
        # capability is off (mirrors the tunnel manager's ``enabled`` gate).
        return bool(self._base_host)

    def _free_port(self) -> int:
        used = {a.port for a in self._by_sid.values()}
        for port in range(self._low, self._high + 1):
            if port not in used:
                return port
        raise PreviewError("preview port pool exhausted")

    def ensure(self, sid: str) -> Allocation:
        """Idempotently reserve this session's port + hostname. One preview per
        session: a repeat call returns the SAME allocation."""
        if not self.enabled:
            raise PreviewError("web preview is not enabled")
        existing = self._by_sid.get(sid)
        if existing is not None:
            _log.debug("host preview reuse sid=%s port=%d", sid, existing.port)
            return existing
        # A 64-bit token never collides in practice, but a collision would silently
        # re-point `resolve(slug)` at a different session (a route hijack, not a benign
        # retry) while `_by_sid` keeps both — so make it impossible, not improbable.
        slug = secrets.token_hex(8)
        while slug in self._by_slug:
            slug = secrets.token_hex(8)
        alloc = Allocation(
            port=self._free_port(),
            slug=slug,
            url=f"https://{slug}-preview.{self._base_host}",
        )
        self._by_sid[sid] = alloc
        self._by_slug[slug] = sid
        _log.info(
            "host preview allocated sid=%s port=%d host=%s-preview.%s",
            sid,
            alloc.port,
            slug,
            self._base_host,
        )
        return alloc

    def port_for(self, sid: str) -> int | None:
        """The session's reserved dev port (what the shell binds via ``$PORT``)."""
        alloc = self._by_sid.get(sid)
        return alloc.port if alloc else None

    def url(self, sid: str) -> str | None:
        alloc = self._by_sid.get(sid)
        return alloc.url if alloc else None

    def resolve(self, slug: str) -> str | None:
        """slug → sid, for the Wave P2 proxy to route an incoming preview host to
        the session whose dev port it should forward to."""
        return self._by_slug.get(slug)

    def release(self, sid: str) -> None:
        alloc = self._by_sid.pop(sid, None)
        if alloc is not None:
            self._by_slug.pop(alloc.slug, None)
            _log.debug("host preview released sid=%s port=%d", sid, alloc.port)

    def release_all(self) -> None:
        for sid in list(self._by_sid):
            self.release(sid)
