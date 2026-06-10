"""Docker access boundary: nothing outside this module touches the docker SDK.

The gateway exposes a deliberately fixed command surface (list, restart,
logs, log stream) so the HTTP layer cannot grow into a shell passthrough,
and so tests can substitute a fake without a docker daemon.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

    import docker
    from docker.models.containers import Container

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

# Docker reports this zero-value timestamp for containers that never started.
_NEVER_STARTED = "0001-01-01T00:00:00Z"


class UnknownServiceError(LookupError):
    """No container in the compose project carries this service label."""

    def __init__(self, service: str) -> None:
        super().__init__(service)
        self.service = service


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """Status snapshot of one compose-managed container."""

    service: str
    state: str
    health: str | None
    started_at: str | None
    image: str


class DockerGateway(Protocol):
    """The full set of Docker operations the supervisor is allowed to perform."""

    def list_containers(self) -> list[ContainerInfo]: ...

    def restart(self, service: str) -> None: ...

    def logs(self, service: str, tail: int) -> str: ...

    def stream_logs(self, service: str) -> Iterator[str]: ...


class ComposeDockerGateway:
    """DockerGateway backed by the docker SDK, scoped to one compose project.

    Scoping is enforced by label filters on every lookup, so containers
    outside the project are invisible and uncontrollable by construction.
    """

    def __init__(self, client: docker.DockerClient, project: str) -> None:
        self._client = client
        self._project = project

    def list_containers(self) -> list[ContainerInfo]:
        containers = self._client.containers.list(
            all=True,
            filters={"label": f"{COMPOSE_PROJECT_LABEL}={self._project}"},
        )
        infos: list[ContainerInfo] = []
        for container in containers:
            service = (container.labels or {}).get(COMPOSE_SERVICE_LABEL)
            if not service:
                continue
            infos.append(_to_info(service, container))
        return infos

    def restart(self, service: str) -> None:
        self._find(service).restart()

    def logs(self, service: str, tail: int) -> str:
        raw: bytes = self._find(service).logs(tail=tail)
        return raw.decode("utf-8", errors="replace")

    def stream_logs(self, service: str) -> Iterator[str]:
        # tail=0: the stream carries only lines emitted after the client attaches.
        chunks = self._find(service).logs(stream=True, follow=True, tail=0)
        return _decode_lines(chunks)

    def _find(self, service: str) -> Container:
        matches = self._client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{COMPOSE_PROJECT_LABEL}={self._project}",
                    f"{COMPOSE_SERVICE_LABEL}={service}",
                ]
            },
        )
        if not matches:
            raise UnknownServiceError(service)
        return matches[0]


def _to_info(service: str, container: Container) -> ContainerInfo:
    attrs = container.attrs or {}
    state = attrs.get("State", {})
    started_at = state.get("StartedAt")
    return ContainerInfo(
        service=service,
        state=state.get("Status", "unknown"),
        health=(state.get("Health") or {}).get("Status"),
        started_at=None if started_at == _NEVER_STARTED else started_at,
        image=attrs.get("Config", {}).get("Image", ""),
    )


def _decode_lines(chunks: Iterator[bytes]) -> Iterator[str]:
    # Docker yields arbitrary byte chunks, not lines; reassemble before decoding.
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")
    if buffer:
        yield buffer.decode("utf-8", errors="replace")
