"""Docker access boundary: nothing outside this module touches the docker SDK.

The gateway exposes a deliberately fixed command surface (list, restart,
logs, log stream) so the HTTP layer cannot grow into a shell passthrough,
and so tests can substitute a fake without a docker daemon.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

    import docker
    from docker.models.containers import Container

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

# Updater one-shots are deliberately OUTSIDE the compose project label so
# stack-wide restarts never touch a running update.
UPDATER_LABEL = "jbrain.updater"
# Export/import one-shots share the updater's detached-container pattern but
# carry their kind as the label value so each has its own status lookup.
ONESHOT_LABEL = "jbrain.oneshot"
UPDATER_IMAGE = "docker:cli"
# The container has docker+compose; git arrives via apk (the update needs
# network for `git pull` anyway, so this adds no new failure class).
UPDATE_COMMAND = (
    "apk add --no-cache git >/dev/null 2>&1 && exec sh src/deploy/update-inner.sh"
)
EXPORT_COMMAND = "exec sh src/deploy/export-inner.sh"
# Reset lives here, not in the api: TRUNCATE needs table ownership and RLS
# does not bind it, so the api's least-privilege role cannot erase data —
# only a supervisor one-shot running superuser psql can.
RESET_COMMAND = "exec sh src/deploy/reset-inner.sh"

# Docker reports this zero-value timestamp for containers that never started.
_NEVER_STARTED = "0001-01-01T00:00:00Z"


class UnknownServiceError(LookupError):
    """No container in the compose project carries this service label."""

    def __init__(self, service: str) -> None:
        super().__init__(service)
        self.service = service


class UpdateInProgressError(RuntimeError):
    """A one-shot (update, export, import, or reset) is already running.

    One-shots are mutually exclusive: an import mid-update, an export
    mid-import, or a reset mid-anything would race over the same database
    and files.
    """


@dataclass(frozen=True, slots=True)
class ContainerMemory:
    """Instantaneous memory usage of one compose container."""

    service: str
    mem_bytes: int


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """State of the most recent updater run ('none' when never run)."""

    state: str  # 'none' | 'running' | 'exited'
    exit_code: int | None
    log_tail: str


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

    def container_memory(self) -> list[ContainerMemory]: ...

    def start_update(self) -> str: ...

    def update_status(self, tail: int) -> UpdateStatus: ...

    def start_export(self) -> str: ...

    def start_import(self, archive: str) -> str: ...

    def start_reset(self) -> str: ...

    def oneshot_status(self, kind: str, tail: int) -> UpdateStatus: ...


class ComposeDockerGateway:
    """DockerGateway backed by the docker SDK, scoped to one compose project.

    Scoping is enforced by label filters on every lookup, so containers
    outside the project are invisible and uncontrollable by construction.
    """

    def __init__(
        self, client: docker.DockerClient, project: str, project_dir: str
    ) -> None:
        self._client = client
        self._project = project
        # Host path of the deploy dir; the updater mounts it at the SAME
        # path so compose's relative binds resolve to real host paths.
        self._project_dir = project_dir

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

    def container_memory(self) -> list[ContainerMemory]:
        usages: list[ContainerMemory] = []
        for container in self._client.containers.list(
            filters={"label": f"{COMPOSE_PROJECT_LABEL}={self._project}"}
        ):
            service = (container.labels or {}).get(COMPOSE_SERVICE_LABEL)
            if not service:
                continue
            try:
                # one_shot skips the 1s CPU sampling window; memory is instant.
                # docker-py types stats() as Iterator; stream=False returns a dict.
                stats = cast("dict", container.stats(stream=False, one_shot=True))
            except Exception:
                continue
            mem = stats.get("memory_stats", {})
            usage = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
            usages.append(ContainerMemory(service=service, mem_bytes=max(usage, 0)))
        return usages

    def start_update(self) -> str:
        return self._run_oneshot("jbrain-updater", {UPDATER_LABEL: "1"}, UPDATE_COMMAND)

    def update_status(self, tail: int) -> UpdateStatus:
        return self._status_of(self._latest(f"{UPDATER_LABEL}=1"), tail)

    def start_export(self) -> str:
        return self._run_oneshot(
            "jbrain-export", {ONESHOT_LABEL: "export"}, EXPORT_COMMAND
        )

    def start_import(self, archive: str) -> str:
        # The archive name is validated at the HTTP layer; quoting here keeps
        # this boundary safe even if a new caller forgets.
        command = f"exec sh src/deploy/import-inner.sh {shlex.quote(archive)}"
        return self._run_oneshot("jbrain-import", {ONESHOT_LABEL: "import"}, command)

    def start_reset(self) -> str:
        return self._run_oneshot(
            "jbrain-reset", {ONESHOT_LABEL: "reset"}, RESET_COMMAND
        )

    def oneshot_status(self, kind: str, tail: int) -> UpdateStatus:
        return self._status_of(self._latest(f"{ONESHOT_LABEL}={kind}"), tail)

    def _run_oneshot(self, prefix: str, labels: dict[str, str], command: str) -> str:
        if self._oneshot_running():
            raise UpdateInProgressError
        name = f"{prefix}-{int(time.time())}"
        self._client.containers.run(
            UPDATER_IMAGE,
            command=["sh", "-lc", command],
            name=name,
            detach=True,
            labels=labels,
            working_dir=self._project_dir,
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                self._project_dir: {"bind": self._project_dir, "mode": "rw"},
            },
        )
        return name

    def _oneshot_running(self) -> bool:
        for label in (f"{UPDATER_LABEL}=1", ONESHOT_LABEL):
            latest = self._latest(label)
            if latest is not None and (latest.attrs or {}).get("State", {}).get(
                "Running"
            ):
                return True
        return False

    def _status_of(self, container: Container | None, tail: int) -> UpdateStatus:
        if container is None:
            return UpdateStatus(state="none", exit_code=None, log_tail="")
        state = (container.attrs or {}).get("State", {})
        running = bool(state.get("Running"))
        raw: bytes = container.logs(tail=tail)
        return UpdateStatus(
            state="running" if running else "exited",
            exit_code=None if running else state.get("ExitCode"),
            log_tail=raw.decode("utf-8", errors="replace"),
        )

    def _latest(self, label: str) -> Container | None:
        matches = self._client.containers.list(all=True, filters={"label": label})
        if not matches:
            return None
        return max(matches, key=lambda c: (c.attrs or {}).get("Created", ""))

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
