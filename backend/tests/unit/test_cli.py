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


def test_smoketest_skips_when_hosting_off(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    # Hosting off or nothing installed → nothing to test, exit 0 (the update path keeps the
    # floated build). No gateway client is even constructed.
    class _Settings:
        local_llm_enabled = False
        local_models: list[str] = []
        local_llm_url = "http://local-llm:8080/v1"

    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())
    assert cli.main(["local-llm-smoketest"]) == 0
    assert "skipping" in capsys.readouterr().out


def test_smoketest_exit_code_follows_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI maps run_smoketest's ok flag to the process exit code the update path reads:
    # 0 keeps the newest build, 1 triggers the rollback.
    import jbrain.llm.local_gateway as gateway_mod
    import jbrain.llm.smoketest as smoketest_mod

    class _Settings:
        local_llm_enabled = True
        local_models = ["gpt-oss-120b", "qwen3.5-0.8b"]
        local_llm_url = "http://local-llm:8080/v1"

    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())
    monkeypatch.setattr(gateway_mod, "LocalGatewayClient", lambda url: object())

    async def _pass(models: object, gw: object) -> tuple[bool, list[str]]:
        return True, ["load OK"]

    async def _fail(models: object, gw: object) -> tuple[bool, list[str]]:
        return False, ["load FAILED"]

    monkeypatch.setattr(smoketest_mod, "run_smoketest", _pass)
    assert cli.main(["local-llm-smoketest"]) == 0
    monkeypatch.setattr(smoketest_mod, "run_smoketest", _fail)
    assert cli.main(["local-llm-smoketest"]) == 1


def test_local_remove_ids_prints_queue(patched_store: FakeSettingsStore, capsys: Any) -> None:
    patched_store.values["llm_local_remove_requested"] = ["gpt-oss-120b", "glm-4.5-air"]
    assert cli.main(["local-remove-ids"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["gpt-oss-120b", "glm-4.5-air"]


def test_local_remove_clear_empties_queue(patched_store: FakeSettingsStore) -> None:
    patched_store.values["llm_local_remove_requested"] = ["gpt-oss-120b"]
    assert cli.main(["local-remove-clear"]) == 0
    assert patched_store.values["llm_local_remove_requested"] == []
