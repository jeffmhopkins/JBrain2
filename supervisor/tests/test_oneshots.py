"""Export/import/reset one-shot endpoints: triggers, validation, mutual exclusion."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH


def test_export_import_require_token(client: TestClient) -> None:
    assert client.post("/export").status_code == 401
    assert client.get("/export/status").status_code == 401
    assert client.post("/import", json={"archive": "x"}).status_code == 401
    assert client.get("/import/status").status_code == 401
    assert client.post("/reset").status_code == 401
    assert client.get("/reset/status").status_code == 401


def test_export_starts_detached_oneshot(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/export", headers=AUTH)
    assert resp.status_code == 202
    assert resp.json()["oneshot"].startswith("jbrain-export-")
    assert ("export", None) in gateway.oneshots_started


def test_import_passes_validated_archive(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    name = "import-20260610-134500.jbrain.tar"
    resp = client.post("/import", json={"archive": name}, headers=AUTH)
    assert resp.status_code == 202
    assert ("import", name) in gateway.oneshots_started


def test_import_rejects_unsafe_archive_names(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    for bad in (
        "../../etc/passwd",
        "import-20260610-134500.jbrain.tar; rm -rf /",
        "export-20260610-134500.jbrain.tar",
        "import-x.jbrain.tar",
        "",
    ):
        resp = client.post("/import", json={"archive": bad}, headers=AUTH)
        assert resp.status_code == 400, bad
    assert gateway.oneshots_started == []


def test_reset_starts_detached_oneshot(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/reset", headers=AUTH)
    assert resp.status_code == 202
    assert resp.json()["oneshot"].startswith("jbrain-reset-")
    assert ("reset", None) in gateway.oneshots_started


def test_oneshots_and_update_exclude_each_other(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    assert client.post("/export", headers=AUTH).status_code == 202
    assert client.post("/export", headers=AUTH).status_code == 409
    assert (
        client.post(
            "/import",
            json={"archive": "import-20260610-134500.jbrain.tar"},
            headers=AUTH,
        ).status_code
        == 409
    )
    assert client.post("/reset", headers=AUTH).status_code == 409
    assert client.post("/update", headers=AUTH).status_code == 409

    gateway.oneshot_running = None
    gateway.updater_running = True
    assert client.post("/export", headers=AUTH).status_code == 409
    assert client.post("/reset", headers=AUTH).status_code == 409


def test_running_reset_blocks_every_other_oneshot(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    assert client.post("/reset", headers=AUTH).status_code == 202
    assert client.post("/reset", headers=AUTH).status_code == 409
    assert client.post("/export", headers=AUTH).status_code == 409
    assert (
        client.post(
            "/import",
            json={"archive": "import-20260610-134500.jbrain.tar"},
            headers=AUTH,
        ).status_code
        == 409
    )
    assert client.post("/update", headers=AUTH).status_code == 409


def test_status_lifecycle_per_kind(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/export/status", headers=AUTH).json()["state"] == "none"

    client.post("/export", headers=AUTH)
    assert client.get("/export/status", headers=AUTH).json()["state"] == "running"
    # The import status is independent of the export's.
    assert client.get("/import/status", headers=AUTH).json()["state"] == "none"

    gateway.oneshot_running = None
    done = client.get("/export/status", headers=AUTH).json()
    assert done["state"] == "exited"
    assert done["exit_code"] == 0


def test_reset_status_lifecycle(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/reset/status", headers=AUTH).json()["state"] == "none"

    client.post("/reset", headers=AUTH)
    running = client.get("/reset/status", headers=AUTH).json()
    assert running["state"] == "running"
    assert "[reset]" in running["log_tail"]
    # Other kinds keep their own status while a reset runs.
    assert client.get("/export/status", headers=AUTH).json()["state"] == "none"

    gateway.oneshot_running = None
    done = client.get("/reset/status", headers=AUTH).json()
    assert done["state"] == "exited"
    assert done["exit_code"] == 0
