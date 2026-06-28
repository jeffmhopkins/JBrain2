"""The preview tunnel manager: one ephemeral tunnel per session, off when disabled."""

from __future__ import annotations

import pytest

from jcode_ctl.preview import (
    CloudflaredTunnel,
    FakeTunnel,
    PreviewError,
    PreviewManager,
)


def _mgr(enabled: bool = True, **kw) -> tuple[PreviewManager, list[FakeTunnel]]:
    made: list[FakeTunnel] = []

    def make() -> FakeTunnel:
        t = FakeTunnel()
        made.append(t)
        return t

    return PreviewManager(make, enabled=enabled, **kw), made


async def test_open_returns_url_and_uses_default_port() -> None:
    mgr, made = _mgr()
    url = await mgr.open("s1")
    assert url == "https://demo-7f3a2c.trycloudflare.com"
    assert mgr.url("s1") == url
    assert made[0].opened_port == 5173


async def test_open_honours_an_explicit_port() -> None:
    mgr, made = _mgr(default_port=5173)
    await mgr.open("s1", 3000)
    assert made[0].opened_port == 3000


async def test_reopen_replaces_the_prior_tunnel() -> None:
    mgr, made = _mgr()
    await mgr.open("s1")
    await mgr.open("s1")
    assert made[0].closed is True  # the first tunnel was torn down
    assert len(made) == 2


async def test_close_tears_down_and_forgets() -> None:
    mgr, made = _mgr()
    await mgr.open("s1")
    await mgr.close("s1")
    assert made[0].closed is True
    assert mgr.url("s1") is None


async def test_close_all() -> None:
    mgr, made = _mgr()
    await mgr.open("s1")
    await mgr.open("s2")
    await mgr.close_all()
    assert all(t.closed for t in made)
    assert mgr.url("s1") is None and mgr.url("s2") is None


async def test_disabled_manager_refuses() -> None:
    mgr, _ = _mgr(enabled=False)
    assert mgr.enabled is False
    with pytest.raises(PreviewError, match="not enabled"):
        await mgr.open("s1")


def test_tunnel_rewrites_origin_host_header_to_localhost() -> None:
    # Without this, cloudflared passes the public trycloudflare Host to the dev
    # server and host-pinning frameworks (Vite 6+) reject it — the tunnel opens
    # but the app stays blank.
    args = CloudflaredTunnel._args(5173)
    assert "--url" in args
    assert args[args.index("--url") + 1] == "http://localhost:5173"
    assert args[args.index("--http-host-header") + 1] == "localhost:5173"


# A trimmed cloudflared quick-tunnel log: the URL banner, then a registered connection.
_URL_LINE = "INF |  https://micro-quite-recorder-allen.trycloudflare.com  |"
_REGISTERED_LINE = "INF Registered tunnel connection connIndex=0 connection=abc"


def test_scan_waits_for_a_registered_connection_before_calling_a_url_ready() -> None:
    # The URL alone isn't enough — cloudflared prints it before the edge connection
    # is up, and the hostname won't resolve until then. _scan must not call it ready.
    url, ready = CloudflaredTunnel._scan([_URL_LINE])
    assert url == "https://micro-quite-recorder-allen.trycloudflare.com"
    assert ready is False

    url, ready = CloudflaredTunnel._scan([_URL_LINE, _REGISTERED_LINE])
    assert url == "https://micro-quite-recorder-allen.trycloudflare.com"
    assert ready is True


def test_failure_message_quotes_cloudflared_output() -> None:
    # URL printed but never registered → name that exact failure and quote the log tail.
    msg = CloudflaredTunnel._failure([_URL_LINE, "ERR failed to dial to edge"])
    assert "no edge connection registered" in msg
    assert "failed to dial to edge" in msg

    # No URL at all → the other failure mode, still with the tail for diagnosis.
    msg = CloudflaredTunnel._failure(["ERR registration rate limited"])
    assert "did not report a tunnel URL" in msg
    assert "rate limited" in msg


async def test_open_and_close_log_the_tunnel_lifecycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Verbose-by-debug-access relies on these lines existing: opening and closing a
    # preview each emit an INFO line the owner debug console can pull.
    mgr, _ = _mgr()
    with caplog.at_level("INFO", logger="jcode_ctl.preview"):
        await mgr.open("s1")
        await mgr.close("s1")
    messages = [r.getMessage() for r in caplog.records]
    assert any("preview open sid=s1" in m for m in messages)
    assert any("preview close sid=s1" in m for m in messages)


async def test_failed_open_tears_down_the_tunnel() -> None:
    """If open() fails after the process spawned, the tunnel is closed, not leaked."""
    closed: list[bool] = []

    class Boom:
        async def open(self, port: int) -> str:
            raise RuntimeError("boom")

        async def close(self) -> None:
            closed.append(True)

    mgr = PreviewManager(Boom, enabled=True)
    with pytest.raises(RuntimeError, match="boom"):
        await mgr.open("s1")
    assert closed == [True]
    assert mgr.url("s1") is None
