"""The box's public base URL (docs/plans/OWNER_TUNNEL_PLAN.md §3.6, wave A5c): only an
https origin is accepted, and the owner's setting wins over the env's."""

import asyncio
from typing import Any

import pytest

from jbrain import public_base
from jbrain.config import Settings
from jbrain.db.session import SessionContext

_DB = "postgresql+asyncpg://nobody@localhost:1/none"


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("", ""),
        ("  ", ""),
        ("https://brain.example.net", "https://brain.example.net"),
        ("https://brain.example.net/", "https://brain.example.net"),
        ("https://brain.example.net:8443", "https://brain.example.net:8443"),
        ("https://[2001:db8::1]", "https://[2001:db8::1]"),
    ],
)
def test_an_https_origin_is_kept(raw: str, want: str) -> None:
    assert public_base.normalize(raw) == want


@pytest.mark.parametrize(
    "raw",
    [
        "http://brain.example.net",
        "brain.example.net",
        "https://",
        "https://brain.example.net/share",
        "https://brain.example.net?x=1",
        "https://brain.example.net#x",
        "https://user:pw@brain.example.net",
        "https://brain.example.net:port",
        "javascript:alert(1)",
        "https://evil.example\\x",
        "https://evil.example%2f",
        "https://ev il.example",
    ],
)
def test_anything_else_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        public_base.normalize(raw)


class _Store:
    def __init__(self, value: Any = None, fail: bool = False) -> None:
        self.value, self.fail = value, fail
        self.ctxs: list[SessionContext] = []

    async def get(self, ctx: SessionContext, key: str, default: Any = None) -> Any:
        self.ctxs.append(ctx)
        if self.fail:
            raise RuntimeError("db down")
        assert key == public_base.PUBLIC_BASE_URL_KEY
        return default if self.value is None else self.value


def _settings(env: str) -> Settings:
    return Settings(database_url=_DB, public_base_url=env)


def test_the_owners_setting_wins_over_the_env() -> None:
    store = _Store("https://brain.example.net")
    assert asyncio.run(public_base.resolve(store, _settings("https://old.example/"))) == (
        "https://brain.example.net"
    )
    assert store.ctxs[0].principal_kind == "owner"


@pytest.mark.parametrize("store", [_Store(), _Store(""), _Store(7), _Store(fail=True)])
def test_the_env_is_the_fallback(store: _Store) -> None:
    assert asyncio.run(public_base.resolve(store, _settings(" https://old.example/ "))) == (
        "https://old.example"
    )
