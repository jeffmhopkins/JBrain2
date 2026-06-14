"""The /api/settings/llm surface — runtime per-task LLM routing + reasoning
effort — with the store faked; the SQL store's round-trip is covered in
test_settings_pg."""

import asyncio
from collections.abc import Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.llm.router import TASK_DEFAULTS
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeSettingsStore]]:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    auth_repo = FakeAuthRepo()
    store = FakeSettingsStore()
    with TestClient(app) as test_client:
        app.state.auth_repo = auth_repo
        app.state.settings_store = store
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            test_client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield test_client, store


def test_requires_auth() -> None:
    app = create_app(
        Settings(secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none")
    )
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/llm").status_code == 401


def test_get_defaults_grok_and_low_for_empty_store(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, _ = client
    body = c.get("/api/settings/llm").json()
    assert body["reasoning_efforts"] == ["none", "low", "medium", "high"]
    assert body["reasoning_default"] == "low"
    assert {p["id"] for p in body["providers"]} == {"grok", "claude", "local"}
    grok = next(p for p in body["providers"] if p["id"] == "grok")
    assert grok["supports_reasoning"] is True
    # Every routed task lists with the grok default + the default effort.
    assert {t["id"] for t in body["tasks"]} == set(TASK_DEFAULTS)
    for task in body["tasks"]:
        assert task["provider"] == "grok"
        assert task["reasoning_effort"] == "low"


def test_put_round_trips_effective_values(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    resp = c.put(
        "/api/settings/llm",
        json={
            "tasks": {
                "agent.turn": {"provider": "grok", "reasoning_effort": "high"},
                "note.extract": {"provider": "claude", "reasoning_effort": "high"},
            }
        },
    )
    assert resp.status_code == 200
    tasks = {t["id"]: t for t in resp.json()["tasks"]}
    # grok keeps the stored effort; claude is non-reasoning so effort is null.
    assert tasks["agent.turn"]["provider"] == "grok"
    assert tasks["agent.turn"]["reasoning_effort"] == "high"
    assert tasks["note.extract"]["provider"] == "claude"
    assert tasks["note.extract"]["reasoning_effort"] is None
    # Stored shape: claude drops reasoning_effort entirely.
    stored = cast(dict[str, object], store.values["llm_task_overrides"])
    assert stored["agent.turn"] == {"spec": "xai:grok-4.3", "reasoning_effort": "high"}
    assert stored["note.extract"] == {"spec": "anthropic:claude-sonnet-4-6"}
    # GET reflects the same effective values.
    got = {t["id"]: t for t in c.get("/api/settings/llm").json()["tasks"]}
    assert got["agent.turn"]["reasoning_effort"] == "high"
    assert got["note.extract"]["provider"] == "claude"


def test_put_rejects_unknown_task_provider_and_effort(
    client: tuple[TestClient, FakeSettingsStore],
) -> None:
    c, store = client
    bad_bodies = [
        {"tasks": {"nope.task": {"provider": "grok", "reasoning_effort": "low"}}},
        {"tasks": {"agent.turn": {"provider": "gpt", "reasoning_effort": "low"}}},
        {"tasks": {"agent.turn": {"provider": "grok", "reasoning_effort": "extreme"}}},
        {"tasks": {"agent.turn": {"provider": "grok", "reasoning_effort": "low", "x": 1}}},
    ]
    for body in bad_bodies:
        assert c.put("/api/settings/llm", json=body).status_code == 422
    assert "llm_task_overrides" not in store.values  # nothing leaked
