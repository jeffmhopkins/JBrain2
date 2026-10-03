"""The engine-switch hold (FLASH_NEXT_ENGINE_PLAN F3a): while the api's switch holds
it, the supervisor refuses every engine start but the switch's own, engine restarts,
and every one-shot — so nothing that never asked the api can start an engine under a
switch."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from supervisor import app as app_module
from supervisor.gateway import ContainerInfo
from tests.conftest import AUTH, FakeGateway

SWITCH = "a1b2c3d4e5f6"


def _engines(
    gateway: FakeGateway, standard: str = "exited", flash: str = "exited"
) -> None:
    for service, state in (("local-llm", standard), ("flash-next", flash)):
        gateway.containers.append(
            ContainerInfo(
                service=service, state=state, health=None, started_at=None, image="i"
            )
        )


def _hold(client: TestClient, switch_id: str = SWITCH, ttl: float = 60.0) -> int:
    return client.post(
        "/engine-switch/hold", json={"id": switch_id, "ttl_s": ttl}, headers=AUTH
    ).status_code


def test_hold_routes_need_the_token(client: TestClient) -> None:
    assert (
        client.post("/engine-switch/hold", json={"id": SWITCH, "ttl_s": 5}).status_code
        == 401
    )
    assert client.get("/engine-switch").status_code == 401


def test_a_held_switch_refuses_every_other_engine_start(
    client: TestClient, gateway: FakeGateway
) -> None:
    _engines(gateway, standard="running")
    assert _hold(client) == 200
    assert client.get("/engine-switch", headers=AUTH).json()["held"] is True

    resp = client.post("/start", json={"service": "flash-next"}, headers=AUTH)
    assert resp.status_code == 409 and "engine switch" in resp.json()["detail"]
    resp = client.post("/restart", json={"service": "local-llm"}, headers=AUTH)
    assert resp.status_code == 409
    resp = client.post("/restart", json={"service": "all"}, headers=AUTH)
    assert resp.status_code == 202 and "local-llm" not in resp.json()["restarting"]
    assert gateway.started == [] and "local-llm" not in gateway.restarted

    # The switch's own start, carrying its id, passes once the other engine is down (the
    # one-engine guard still applies to it).
    gateway.containers = [
        replace(c, state="exited") if c.service == "local-llm" else c
        for c in gateway.containers
    ]
    resp = client.post(
        "/start", json={"service": "flash-next", "switch_id": SWITCH}, headers=AUTH
    )
    assert resp.status_code == 202 and gateway.started == ["flash-next"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/update", None),
        ("/export", None),
        ("/reset", None),
        ("/provision", None),
        ("/rebuild", {"service": "api"}),
        ("/refresh", {"service": "api"}),
        ("/import", {"archive": "import-20260101-000000.jbrain.tar"}),
    ],
)
def test_a_held_switch_refuses_every_oneshot(
    client: TestClient, gateway: FakeGateway, path: str, body: dict | None
) -> None:
    assert _hold(client) == 200
    resp = client.post(path, json=body, headers=AUTH)
    assert resp.status_code == 409 and "engine switch" in resp.json()["detail"]
    assert gateway.updates_started == [] and gateway.oneshots_started == []


def test_the_hold_is_exclusive_renewable_and_released_only_by_its_holder(
    client: TestClient,
) -> None:
    assert _hold(client) == 200
    assert _hold(client) == 200  # renewal
    assert _hold(client, "ffffffff0000") == 409
    release = {"id": "ffffffff0000"}
    assert client.post("/engine-switch/release", json=release, headers=AUTH).json()[
        "held"
    ]
    resp = client.post("/engine-switch/release", json={"id": SWITCH}, headers=AUTH)
    assert resp.json()["held"] is False
    assert _hold(client, "ffffffff0000") == 200


def test_the_hold_refuses_to_start_under_a_oneshot(
    client: TestClient, gateway: FakeGateway
) -> None:
    gateway.oneshot_running = "refresh"
    assert _hold(client) == 409


def test_the_hold_lapses_at_its_deadline(
    client: TestClient, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _engines(gateway)
    now = {"t": 1000.0}
    monkeypatch.setattr(app_module.time, "monotonic", lambda: now["t"])
    assert _hold(client, ttl=30.0) == 200
    assert (
        client.post("/start", json={"service": "local-llm"}, headers=AUTH).status_code
        == 409
    )
    now["t"] += 31.0
    assert client.get("/engine-switch", headers=AUTH).json()["held"] is False
    assert (
        client.post("/start", json={"service": "local-llm"}, headers=AUTH).status_code
        == 202
    )


def test_bad_hold_requests_are_422(client: TestClient) -> None:
    for body in (
        {"id": "not hex!", "ttl_s": 5},
        {"id": SWITCH, "ttl_s": 0},
        {"id": SWITCH, "ttl_s": 99999},
        {"id": SWITCH, "ttl_s": 5, "extra": 1},
    ):
        assert (
            client.post("/engine-switch/hold", json=body, headers=AUTH).status_code
            == 422
        )
