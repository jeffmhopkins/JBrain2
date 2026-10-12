"""Shared fixtures: a fake gateway and a wired TestClient — no docker daemon."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from supervisor.app import create_app
from supervisor.config import Settings
from supervisor.gateway import (
    ContainerInfo,
    ContainerMemory,
    ProcessMemory,
    UnknownServiceError,
    UpdateInProgressError,
    UpdateStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# The Minecraft wrapper (deploy/minecraft/server.py) roots every file it keeps at
# MC_DATA_DIR, /data by default: the live box's volume. Set before any test module
# loads it, so even a path a test forgot (or a thread that outlives its test's patches)
# lands in this sentinel, never /data; _mc_sentinel_stays_empty fails the run if
# anything does.
MC_SENTINEL = Path(tempfile.mkdtemp(prefix="mc-data-sentinel-"))
os.environ["MC_DATA_DIR"] = str(MC_SENTINEL)
MC_SERVER_PY = Path(__file__).resolve().parents[2] / "deploy/minecraft/server.py"
# Every module-level path in server.py, as it is under DATA.
MC_DATA_PATHS = {
    "DATA": "",
    "SERVER_DIR": "server",
    "SNAPSHOT_DIR": "snapshots",
    "OVERRIDES": "properties.json",
    "SETTINGS": "settings.json",
    "SLOTS": "slots.json",
    "SNAPSHOT_INDEX": "snapshot-index.json",
    "UPLOAD_DIR": "uploads",
    "STRUCTURE_DIR": "structures",
}

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
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.log_requests: list[tuple[str, int]] = []
        self.updater_running = False
        self.updates_started: list[str] = []
        self.update_log = "[update] starting"
        self.oneshot_running: str | None = None
        self.oneshots_started: list[tuple[str, str | None]] = []
        self.oneoffs: list[ContainerInfo] = []

    def list_containers(self) -> list[ContainerInfo]:
        return list(self.containers)

    def restart(self, service: str) -> None:
        self._check(service)
        self.restarted.append(service)

    def start(self, service: str) -> None:
        self._check(service)
        self.started.append(service)

    def stop(self, service: str) -> None:
        self._check(service)
        self.stopped.append(service)

    def logs(self, service: str, tail: int) -> str:
        self._check(service)
        self.log_requests.append((service, tail))
        return "\n".join(self.log_lines.get(service, [])[-tail:])

    def stream_logs(self, service: str) -> Iterator[str]:
        self._check(service)
        return iter(self.log_lines.get(service, []))

    def container_memory(self) -> list[ContainerMemory]:
        return [
            ContainerMemory(service=c.service, mem_bytes=100 << 20)
            for c in self.containers
        ]

    def container_processes(self) -> list[ProcessMemory]:
        return [
            ProcessMemory(
                service=c.service,
                pid=1000 + i,
                rss_bytes=50 << 20,
                command=f"/usr/bin/{c.service} --serve",
            )
            for i, c in enumerate(self.containers)
        ]

    def start_update(self) -> str:
        if self._busy():
            raise UpdateInProgressError
        self.updater_running = True
        name = f"jbrain-updater-{len(self.updates_started)}"
        self.updates_started.append(name)
        return name

    def start_export(self) -> str:
        if self._busy():
            raise UpdateInProgressError
        self.oneshot_running = "export"
        self.oneshots_started.append(("export", None))
        return f"jbrain-export-{len(self.oneshots_started)}"

    def start_import(self, archive: str) -> str:
        if self._busy():
            raise UpdateInProgressError
        self.oneshot_running = "import"
        self.oneshots_started.append(("import", archive))
        return f"jbrain-import-{len(self.oneshots_started)}"

    def start_reset(self) -> str:
        if self._busy():
            raise UpdateInProgressError
        self.oneshot_running = "reset"
        self.oneshots_started.append(("reset", None))
        return f"jbrain-reset-{len(self.oneshots_started)}"

    def start_provision(self) -> str:
        if self._busy():
            raise UpdateInProgressError
        self.oneshot_running = "provision"
        self.oneshots_started.append(("provision", None))
        return f"jbrain-provision-{len(self.oneshots_started)}"

    def start_rebuild(self, service: str) -> str:
        if self._busy():
            raise UpdateInProgressError
        self.oneshot_running = "rebuild"
        self.oneshots_started.append(("rebuild", service))
        return f"jbrain-rebuild-{len(self.oneshots_started)}"

    def start_refresh(self, service: str) -> str:
        if self._busy():
            raise UpdateInProgressError
        self.oneshot_running = "refresh"
        self.oneshots_started.append(("refresh", service))
        return f"jbrain-refresh-{len(self.oneshots_started)}"

    def start_perplexity(self, model_path: str, chunks: int | None) -> str:
        if self._busy():
            raise UpdateInProgressError
        self.oneshot_running = "perplexity"
        self.oneshots_started.append(("perplexity", f"{model_path}|{chunks}"))
        return f"jbrain-perplexity-{len(self.oneshots_started)}"

    def oneshot_status(self, kind: str, tail: int) -> UpdateStatus:
        if not any(k == kind for k, _ in self.oneshots_started):
            return UpdateStatus(state="none", exit_code=None, log_tail="")
        if self.oneshot_running == kind:
            return UpdateStatus(
                state="running", exit_code=None, log_tail=f"[{kind}] starting"
            )
        return UpdateStatus(state="exited", exit_code=0, log_tail=f"[{kind}] complete")

    def running_oneshot(self) -> str | None:
        if self.updater_running:
            return "update"
        return self.oneshot_running

    def list_oneoffs(self) -> list[ContainerInfo]:
        return list(self.oneoffs)

    def _busy(self) -> bool:
        return self.updater_running or self.oneshot_running is not None

    def update_status(self, tail: int) -> UpdateStatus:
        if not self.updates_started:
            return UpdateStatus(state="none", exit_code=None, log_tail="")
        if self.updater_running:
            return UpdateStatus(
                state="running", exit_code=None, log_tail=self.update_log
            )
        return UpdateStatus(state="exited", exit_code=0, log_tail=self.update_log)

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
    # watch_api off: route tests have no business starting a background network probe.
    with TestClient(create_app(settings, gateway, watch_api=False)) as test_client:
        yield test_client


def mc_server_modules() -> list[ModuleType]:
    """Every loaded copy of server.py. Test files load it by path, and a later load
    replaces the sys.modules entry while the first file still holds its own copy, so
    the copies are found through every loaded module's globals too."""
    found: dict[int, ModuleType] = {}
    for mod in list(sys.modules.values()):
        for obj in [mod, *list(getattr(mod, "__dict__", {}).values())]:
            if not isinstance(obj, ModuleType):
                continue
            file = getattr(obj, "__file__", None)
            if file and Path(file).resolve() == MC_SERVER_PY:
                found[id(obj)] = obj
    return list(found.values())


@pytest.fixture(scope="session")
def _mc_modules() -> list[ModuleType]:
    # Looked up once: every test file has been imported by the first test.
    return mc_server_modules()


@pytest.fixture(autouse=True)
def _mc_data_in_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _mc_modules: list[ModuleType]
) -> None:
    """Each test's own data dir for every server.py copy, so no test shares state
    through the sentinel or reaches /data. A test may still point one elsewhere."""
    data = tmp_path / "mc-data"
    for mod in _mc_modules:
        for name, rel in MC_DATA_PATHS.items():
            monkeypatch.setattr(mod, name, data / rel if rel else data)


@pytest.fixture(autouse=True, scope="session")
def _mc_sentinel_stays_empty() -> Iterator[None]:
    yield
    leaked = sorted(str(p.relative_to(MC_SENTINEL)) for p in MC_SENTINEL.rglob("*"))
    assert not leaked, (
        f"a test wrote to the Minecraft wrapper's default data dir (on a real run, "
        f"/data): {leaked}"
    )
    MC_SENTINEL.rmdir()
