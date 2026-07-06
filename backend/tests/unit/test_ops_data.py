"""Export/import/reset ops endpoints: proxying, the shelf handoff, and name safety."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.storage import FsBackupShelf
from tests.unit.fakes import FakeAuthRepo

EXPORT_NAME = "export-20260610-120000.jbrain.tar"


class FakeSupervisor:
    """Scripted supervisor: one-shot state is set by each test."""

    def __init__(self) -> None:
        self.export_state = {"state": "none", "exit_code": None, "log_tail": ""}
        self.import_state = {"state": "none", "exit_code": None, "log_tail": ""}
        self.reset_state = {"state": "none", "exit_code": None, "log_tail": ""}
        self.provision_state = {"state": "none", "exit_code": None, "log_tail": ""}
        self.rebuild_state = {"state": "none", "exit_code": None, "log_tail": ""}
        self.busy = False
        self.import_started_with: list[str] = []
        self.resets_started = 0
        self.provisions_started = 0
        self.rebuilt_services: list[str] = []
        self.rebuild_known = {"api", "server-brain", "worker"}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != "Bearer st-token":
            return httpx.Response(401)
        path, method = request.url.path, request.method
        if path == "/export" and method == "POST":
            if self.busy:
                return httpx.Response(409)
            return httpx.Response(202, json={"oneshot": "jbrain-export-1"})
        if path == "/export/status":
            return httpx.Response(200, json=self.export_state)
        if path == "/import" and method == "POST":
            archive = json.loads(request.content)["archive"]
            if not archive.startswith("import-"):
                return httpx.Response(400)
            if self.busy:
                return httpx.Response(409)
            self.import_started_with.append(archive)
            return httpx.Response(202, json={"oneshot": "jbrain-import-1"})
        if path == "/import/status":
            return httpx.Response(200, json=self.import_state)
        if path == "/reset" and method == "POST":
            if self.busy:
                return httpx.Response(409)
            self.resets_started += 1
            return httpx.Response(202, json={"oneshot": "jbrain-reset-1"})
        if path == "/reset/status":
            return httpx.Response(200, json=self.reset_state)
        if path == "/provision" and method == "POST":
            if self.busy:
                return httpx.Response(409)
            self.provisions_started += 1
            return httpx.Response(202, json={"oneshot": "jbrain-provision-1"})
        if path == "/provision/status":
            return httpx.Response(200, json=self.provision_state)
        if path == "/rebuild" and method == "POST":
            service = json.loads(request.content)["service"]
            if service not in self.rebuild_known:
                return httpx.Response(404)
            if self.busy:
                return httpx.Response(409)
            self.rebuilt_services.append(service)
            return httpx.Response(202, json={"oneshot": "jbrain-rebuild-1"})
        if path == "/rebuild/status":
            return httpx.Response(200, json=self.rebuild_state)
        if path == "/metrics":
            return httpx.Response(
                200,
                json={
                    "mem_total_bytes": 130_000_000_000,
                    "mem_available_bytes": 8_000_000_000,
                    "swap_total_bytes": 0,
                    "swap_free_bytes": 0,
                    "disk_total_bytes": 2_000_000_000_000,
                    "disk_free_bytes": 1_200_000_000_000,
                    "load_1m": 0.5,
                    "load_5m": 0.7,
                    "load_15m": 0.8,
                    "uptime_seconds": 86400,
                    "gpu_busy_percent": 1.0,
                    "fan_rpm": None,
                    "apu_power_w": 13.0,
                    "containers": [{"service": "local-llm", "mem_bytes": 100_000_000_000}],
                },
            )
        if path == "/processes":
            return httpx.Response(
                200,
                json={
                    "processes": [
                        {
                            "service": "comfyui",
                            "pid": 201,
                            "rss_bytes": 2_000_000_000,
                            "command": "x",
                        },
                        {
                            "service": "local-llm",
                            "pid": 101,
                            "rss_bytes": 64_000_000_000,
                            "command": "llama-server --model gpt-oss-120b " + "y" * 400,
                        },
                    ]
                },
            )
        return httpx.Response(404)


@pytest.fixture
def supervisor() -> FakeSupervisor:
    return FakeSupervisor()


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def shelf_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture
def client(repo: FakeAuthRepo, supervisor: FakeSupervisor, shelf_dir: Path) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False,
        supervisor_token="st-token",
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        backups_dir=str(shelf_dir),
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.backup_shelf = FsBackupShelf(shelf_dir)
        app.state.supervisor_client = httpx.AsyncClient(
            transport=httpx.MockTransport(supervisor), base_url="http://supervisor"
        )
        login(test_client, repo)
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert (
        client.post(
            "/api/auth/session", json={"owner_key": key, "device_label": "test"}
        ).status_code
        == 204
    )


def test_data_endpoints_require_owner(
    repo: FakeAuthRepo, supervisor: FakeSupervisor, shelf_dir: Path
) -> None:
    settings = Settings(
        secure_cookies=False,
        supervisor_token="st-token",
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        backups_dir=str(shelf_dir),
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = repo
        assert anon.post("/api/ops/export").status_code == 401
        assert anon.get(f"/api/ops/export/file/{EXPORT_NAME}").status_code == 401
        assert anon.post("/api/ops/import/upload").status_code == 401
        assert anon.post("/api/ops/reset").status_code == 401
        assert anon.get("/api/ops/reset/status").status_code == 401


def test_rebuild_requires_owner(
    repo: FakeAuthRepo, supervisor: FakeSupervisor, shelf_dir: Path
) -> None:
    settings = Settings(
        secure_cookies=False,
        supervisor_token="st-token",
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        backups_dir=str(shelf_dir),
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = repo
        assert anon.post("/api/ops/rebuild", json={"service": "api"}).status_code == 401
        assert anon.get("/api/ops/rebuild/status").status_code == 401


def test_rebuild_proxies_the_service(client: TestClient, supervisor: FakeSupervisor) -> None:
    resp = client.post("/api/ops/rebuild", json={"service": "server-brain"})
    assert resp.status_code == 202
    assert resp.json()["oneshot"] == "jbrain-rebuild-1"
    assert supervisor.rebuilt_services == ["server-brain"]


def test_rebuild_unknown_service_is_404(client: TestClient) -> None:
    assert client.post("/api/ops/rebuild", json={"service": "nope"}).status_code == 404


def test_rebuild_conflicts_while_a_oneshot_runs(
    client: TestClient, supervisor: FakeSupervisor
) -> None:
    supervisor.busy = True
    assert client.post("/api/ops/rebuild", json={"service": "api"}).status_code == 409


def test_rebuild_status_proxies(client: TestClient, supervisor: FakeSupervisor) -> None:
    supervisor.rebuild_state = {"state": "running", "exit_code": None, "log_tail": "[rebuild]"}
    body = client.get("/api/ops/rebuild/status").json()
    assert body["state"] == "running"


def test_metrics_merges_per_process_breakdown(client: TestClient) -> None:
    # /ops/metrics folds the supervisor's /processes into the host metrics, biggest
    # first, with the argv clipped — the data the memory-breakdown card reads.
    body = client.get("/api/ops/metrics").json()
    assert body["mem_total_bytes"] == 130_000_000_000
    assert [p["service"] for p in body["processes"]] == ["local-llm", "comfyui"]
    assert len(body["processes"][0]["command"]) <= 200
    # db/blobs are best-effort and unwired in this fake → null, never a 500.
    assert body["db"] is None


def test_export_start_and_busy_conflict(client: TestClient, supervisor: FakeSupervisor) -> None:
    assert client.post("/api/ops/export").status_code == 202
    supervisor.busy = True
    assert client.post("/api/ops/export").status_code == 409


def test_export_status_names_the_file_only_when_done(
    client: TestClient, supervisor: FakeSupervisor, shelf_dir: Path
) -> None:
    running = client.get("/api/ops/export/status").json()
    assert running["filename"] is None

    shelf_dir.mkdir(parents=True)
    (shelf_dir / EXPORT_NAME).write_bytes(b"archive")
    supervisor.export_state = {"state": "exited", "exit_code": 0, "log_tail": "done"}
    done = client.get("/api/ops/export/status").json()
    assert done["filename"] == EXPORT_NAME

    supervisor.export_state = {"state": "exited", "exit_code": 1, "log_tail": "boom"}
    failed = client.get("/api/ops/export/status").json()
    assert failed["filename"] is None


def test_export_download_serves_only_export_archives(client: TestClient, shelf_dir: Path) -> None:
    shelf_dir.mkdir(parents=True)
    (shelf_dir / EXPORT_NAME).write_bytes(b"tar bytes")
    (shelf_dir / "jbrain-20260610.dump").write_bytes(b"nightly dump")

    ok = client.get(f"/api/ops/export/file/{EXPORT_NAME}")
    assert ok.status_code == 200
    assert ok.content == b"tar bytes"

    # Nightly backups and traversal attempts are invisible through this route.
    assert client.get("/api/ops/export/file/jbrain-20260610.dump").status_code == 404
    assert client.get("/api/ops/export/file/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/api/ops/export/file/export-99999999-999999.jbrain.tar").status_code == 404


def test_import_upload_parks_archive_then_start_hands_it_off(
    client: TestClient, supervisor: FakeSupervisor, shelf_dir: Path
) -> None:
    upload = client.post(
        "/api/ops/import/upload",
        files={"file": ("mine.jbrain.tar", b"archive bytes", "application/x-tar")},
    )
    assert upload.status_code == 201
    name = upload.json()["archive"]
    assert name.startswith("import-") and name.endswith(".jbrain.tar")
    assert (shelf_dir / name).read_bytes() == b"archive bytes"

    started = client.post("/api/ops/import/start", json={"archive": name})
    assert started.status_code == 202
    assert supervisor.import_started_with == [name]


def test_import_start_rejects_foreign_names(client: TestClient) -> None:
    resp = client.post("/api/ops/import/start", json={"archive": "../evil.tar"})
    assert resp.status_code == 400


def test_import_status_proxies(client: TestClient, supervisor: FakeSupervisor) -> None:
    supervisor.import_state = {"state": "running", "exit_code": None, "log_tail": "x"}
    assert client.get("/api/ops/import/status").json()["state"] == "running"


def test_reset_start_and_busy_conflict(client: TestClient, supervisor: FakeSupervisor) -> None:
    assert client.post("/api/ops/reset").status_code == 202
    assert supervisor.resets_started == 1

    supervisor.busy = True
    busy = client.post("/api/ops/reset")
    assert busy.status_code == 409
    assert busy.json()["detail"] == "another operation is running"


def test_reset_status_proxies(client: TestClient, supervisor: FakeSupervisor) -> None:
    assert client.get("/api/ops/reset/status").json()["state"] == "none"
    supervisor.reset_state = {
        "state": "exited",
        "exit_code": 0,
        "log_tail": "[reset] complete",
    }
    done = client.get("/api/ops/reset/status").json()
    assert done["state"] == "exited"
    assert done["exit_code"] == 0
    assert "[reset] complete" in done["log_tail"]


def test_local_provision_start_and_busy_conflict(
    client: TestClient, supervisor: FakeSupervisor
) -> None:
    # The "Download" action: trigger the weight-sync one-shot without a system update.
    assert client.post("/api/ops/local-provision").status_code == 202
    assert supervisor.provisions_started == 1

    supervisor.busy = True
    busy = client.post("/api/ops/local-provision")
    assert busy.status_code == 409
    assert busy.json()["detail"] == "another operation is running"


def test_local_provision_status_proxies(client: TestClient, supervisor: FakeSupervisor) -> None:
    assert client.get("/api/ops/local-provision/status").json()["state"] == "none"
    supervisor.provision_state = {
        "state": "exited",
        "exit_code": 1,
        "log_tail": "[local-llm] DOWNLOAD FAILED for qwen3.5-0.8b",
    }
    done = client.get("/api/ops/local-provision/status").json()
    assert done["state"] == "exited"
    assert done["exit_code"] == 1
    assert "DOWNLOAD FAILED" in done["log_tail"]
