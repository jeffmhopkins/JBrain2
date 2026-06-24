"""Unit coverage for the operator CLI's local-model queue commands.

The print/clear branches are the update one-shot's read/clear surface; they run
inside the api container against the owner-scoped settings store. Here we stub the
engine/sessionmaker/store so the command logic is exercised without a real DB.
"""

from typing import Any

import pytest

from jbrain import cli
from tests.unit.fakes import FakeSettingsStore


class _FakeEngine:
    async def dispose(self) -> None:
        return None


@pytest.fixture
def patched_store(monkeypatch: pytest.MonkeyPatch) -> FakeSettingsStore:
    """Wire cli's engine/sessionmaker/store seams to an in-memory FakeSettingsStore."""
    store = FakeSettingsStore()

    class _Settings:
        database_url = "postgresql+asyncpg://x/y"

    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())
    monkeypatch.setattr(cli, "create_async_engine", lambda url: _FakeEngine())
    monkeypatch.setattr(cli, "async_sessionmaker", lambda engine, **kw: object())
    monkeypatch.setattr(cli, "SqlSettingsStore", lambda maker: store)
    return store


def test_local_remove_ids_prints_queue(patched_store: FakeSettingsStore, capsys: Any) -> None:
    patched_store.values["llm_local_remove_requested"] = ["gpt-oss-120b", "glm-4.5-air"]
    assert cli.main(["local-remove-ids"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["gpt-oss-120b", "glm-4.5-air"]


def test_local_remove_clear_empties_queue(patched_store: FakeSettingsStore) -> None:
    patched_store.values["llm_local_remove_requested"] = ["gpt-oss-120b"]
    assert cli.main(["local-remove-clear"]) == 0
    assert patched_store.values["llm_local_remove_requested"] == []
