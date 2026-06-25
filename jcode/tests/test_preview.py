"""The preview tunnel manager: one ephemeral tunnel per session, off when disabled."""

from __future__ import annotations

import pytest

from jcode_ctl.preview import FakeTunnel, PreviewError, PreviewManager


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
