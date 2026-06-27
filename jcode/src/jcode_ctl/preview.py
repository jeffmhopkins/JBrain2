"""Per-session web preview: an ephemeral Cloudflare quick-tunnel to the sandbox's
dev server (docs/proposed/JCODE_PLAN.md, Wave J4).

``cloudflared tunnel --url http://localhost:<port>`` uses TryCloudflare — no
account, no token, no DNS — and returns a random ``*.trycloudflare.com`` URL that
dies when the process exits, so a preview lives exactly as long as its session.
OFF by default: it exposes the running dev app to anyone holding the (unguessable)
URL, so the owner enables it deliberately.

The tunnel is behind a port so the manager is testable with a fake (no subprocess,
no network); the real adapter shells to ``cloudflared`` and is exercised on-box.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from typing import Protocol


class PreviewError(RuntimeError):
    """A preview couldn't be opened (disabled, or the tunnel failed to report)."""


_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class Tunnel(Protocol):
    async def open(self, port: int) -> str: ...

    async def close(self) -> None: ...


class CloudflaredTunnel:
    """Real tunnel (on-box): spawn cloudflared and parse the trycloudflare URL it
    prints. The process IS the tunnel — terminating it tears the tunnel down."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None

    @staticmethod
    def _args(port: int) -> list[str]:
        # cloudflared forwards the public *.trycloudflare.com Host to the origin by
        # default. Dev servers that pin a host allowlist (Vite 6+, webpack-dev-server)
        # reject a foreign Host and serve a blank/"blocked request" page — the tunnel
        # resolves but the app never renders. Rewrite the origin Host to localhost (the
        # value the dev server already trusts) so any framework works through it.
        return [
            "cloudflared",
            "tunnel",
            "--no-autoupdate",
            "--url",
            f"http://localhost:{port}",
            "--http-host-header",
            f"localhost:{port}",
        ]

    async def open(self, port: int) -> str:  # pragma: no cover - exercised on-box
        self._proc = await asyncio.create_subprocess_exec(
            *self._args(port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert self._proc.stdout is not None
        for _ in range(200):
            try:
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=30)
            except TimeoutError:
                break
            if not line:
                break
            match = _URL_RE.search(line.decode(errors="replace"))
            if match:
                return match.group(0)
        await self.close()
        raise PreviewError("cloudflared did not report a tunnel URL")

    async def close(self) -> None:  # pragma: no cover - exercised on-box
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            # cloudflared ignored SIGTERM — escalate to SIGKILL so an internet-facing
            # tunnel can never outlive its close() (review SF2).
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


class FakeTunnel:
    """In-memory tunnel for tests: no subprocess, no network."""

    def __init__(self, url: str = "https://demo-7f3a2c.trycloudflare.com") -> None:
        self._url = url
        self.opened_port: int | None = None
        self.closed = False

    async def open(self, port: int) -> str:
        self.opened_port = port
        return self._url

    async def close(self) -> None:
        self.closed = True


class PreviewManager:
    """Owns one live tunnel per session. Off when ``enabled`` is false (fail-closed)."""

    def __init__(
        self,
        make_tunnel: Callable[[], Tunnel],
        *,
        enabled: bool,
        default_port: int = 5173,
    ) -> None:
        self._make = make_tunnel
        self._enabled = enabled
        self._default_port = default_port
        self._tunnels: dict[str, Tunnel] = {}
        self._urls: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def open(self, sid: str, port: int | None = None) -> str:
        if not self._enabled:
            raise PreviewError("web preview is not enabled")
        await self.close(sid)  # one tunnel per session — replace any prior
        tunnel = self._make()
        try:
            url = await tunnel.open(port or self._default_port)
        except BaseException:
            # Cancelled or failed AFTER the process spawned — tear it down before it
            # leaks: an unregistered tunnel can never be reached by close()/close_all()
            # (review SF1). BaseException so a cancellation is cleaned up too.
            await tunnel.close()
            raise
        self._tunnels[sid] = tunnel
        self._urls[sid] = url
        return url

    def url(self, sid: str) -> str | None:
        return self._urls.get(sid)

    async def close(self, sid: str) -> None:
        tunnel = self._tunnels.pop(sid, None)
        self._urls.pop(sid, None)
        if tunnel is not None:
            await tunnel.close()

    async def close_all(self) -> None:
        for sid in list(self._tunnels):
            await self.close(sid)
