"""Shared fixtures: a fake-driven control app, no git / network."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jcode_ctl.app import create_app
from jcode_ctl.config import Settings
from jcode_ctl.host_preview import HostPreviewManager
from jcode_ctl.sessions import SessionManager
from jcode_ctl.workspace import FakeWorkspace

TOKEN = "test-token"


@pytest.fixture
def manager() -> SessionManager:
    return SessionManager(FakeWorkspace(), "/work", new_id=_ids())


@pytest.fixture
def host_preview() -> HostPreviewManager:
    # Pure in-memory (no subprocess/network), so the real allocator IS the test double.
    return HostPreviewManager(base_host="box.test", port_low=59000, port_high=59010)


@pytest.fixture
def client(manager: SessionManager, host_preview: HostPreviewManager) -> TestClient:
    settings = Settings(token=TOKEN)
    return TestClient(create_app(settings, manager, host_preview))


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _ids():
    counter = {"n": 0}

    def _next() -> str:
        counter["n"] += 1
        return f"sess{counter['n']}"

    return _next
