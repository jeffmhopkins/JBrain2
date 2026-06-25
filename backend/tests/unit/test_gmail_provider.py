"""GmailClientProvider — resolves live credentials (settings store over env) into a
cached client (docs/EMAIL_ARCHIVIST_PLAN.md). No network: client() only constructs."""

from typing import Any

import pytest

from jbrain.config import Settings
from jbrain.gmail import GmailClient, GmailClientProvider, GmailError
from tests.unit.fakes import FakeSettingsStore


def _settings(**kw: Any) -> Settings:
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


def _provider(store: FakeSettingsStore, settings: Settings) -> GmailClientProvider:
    return GmailClientProvider(
        store, settings, base_url="https://g.example/v1", token_url="https://t.example/token"
    )


async def test_not_configured_reports_and_raises() -> None:
    provider = _provider(FakeSettingsStore(), _settings())
    assert await provider.configured() is False
    with pytest.raises(GmailError):
        await provider.client()


async def test_store_credentials_build_a_client() -> None:
    store = FakeSettingsStore()
    await store.set_gmail_credentials(
        None, client_id="cid", client_secret="sec", refresh_token="rt"
    )
    provider = _provider(store, _settings())
    assert await provider.credentials() == ("cid", "sec", "rt")
    assert await provider.configured() is True
    client = await provider.client()
    assert isinstance(client, GmailClient)
    assert await provider.client() is client  # cached while credentials are unchanged


async def test_env_is_the_fallback_when_the_store_is_blank() -> None:
    provider = _provider(
        FakeSettingsStore(),
        _settings(gmail_client_id="ecid", gmail_client_secret="esec", gmail_refresh_token="ert"),
    )
    assert await provider.credentials() == ("ecid", "esec", "ert")
    assert await provider.configured() is True


async def test_store_overrides_env() -> None:
    store = FakeSettingsStore()
    await store.set_gmail_credentials(None, refresh_token="stored")
    provider = _provider(store, _settings(gmail_refresh_token="env"))
    assert (await provider.credentials())[2] == "stored"


async def test_rebuilds_when_credentials_change() -> None:
    store = FakeSettingsStore()
    await store.set_gmail_credentials(None, client_id="a", client_secret="b", refresh_token="r1")
    provider = _provider(store, _settings())
    first = await provider.client()
    await store.set_gmail_credentials(None, refresh_token="r2")
    assert await provider.client() is not first  # a saved change is picked up, no restart
