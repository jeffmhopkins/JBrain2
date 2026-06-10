"""Shared fixtures: a fake gateway and a wired TestClient — no docker daemon."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from supervisor.app import create_app
from supervisor.config import Settings
from supervisor.gateway import ContainerInfo, UnknownServiceError

if TYPE_CHECKING:
    from collections.abc import Iterator

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeGateway:
    """In-memory DockerGateway that records calls for assertions."""

    def __init__(
        self,
        containers: list[ContainerInfo],
        logs: dict[str, list[str]] | None = None,
    ) -> None:
        self.containers = containers
        self.log_lines = logs or {}
        self.restarted: list[str] = []
        self.log_requests: list[tuple[str, int]] = []

    def list_containers(self) -> list[ContainerInfo]:
        return list(self.containers)

    def restart(self, service: str) -> None:
        self._check(service)
        self.restarted.append(service)

    def logs(self, service: str, tail: int) -> str:
        self._check(service)
        self.log_requests.append((service, tail))
        return "\n".join(self.log_lines.get(service, [])[-tail:])

    def stream_logs(self, service: str) -> Iterator[str]:
        self._check(service)
        return iter(self.log_lines.get(service, []))

    def _check(self, service: str) -> None:
        if service not in {c.service for c in self.containers}:
            raise UnknownServiceError(service)


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway(
        containers=[
            ContainerInfo(
                service="api",
                state="running",
                health="healthy",
                started_at="2026-06-10T00:00:00Z",
                image="ghcr.io/jeff/jbrain-api:edge",
            ),
            ContainerInfo(
                service="postgres",
                state="running",
                health="healthy",
                started_at="2026-06-10T00:00:00Z",
                image="postgres:16",
            ),
            ContainerInfo(
                service="supervisor",
                state="running",
                health=None,
                started_at="2026-06-10T00:00:00Z",
                image="ghcr.io/jeff/jbrain-supervisor:edge",
            ),
        ],
        logs={"api": ["line one", "line two", "line three"]},
    )


@pytest.fixture
def client(gateway: FakeGateway) -> Iterator[TestClient]:
    settings = Settings(supervisor_token=TOKEN)
    with TestClient(create_app(settings, gateway)) as test_client:
        yield test_client
