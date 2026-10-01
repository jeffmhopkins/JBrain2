"""The one-engine guard on every route that can START an engine container.

FLASH_NEXT_ENGINE_PLAN §4d: the standard `local-llm` gateway and `flash-next` together
freeze a 128 GB box. The engine that is not selected is deliberately created and left
stopped, so any route that starts a container — /start, and /restart, since `docker
restart` of a stopped container starts it — must refuse to put a second engine up.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from supervisor.app import create_app
from supervisor.config import Settings
from supervisor.gateway import ENGINE_UP_STATES, ContainerInfo, engine_up
from tests.conftest import AUTH, TOKEN, FakeGateway


def _engines(gateway: FakeGateway, standard: str, flash: str) -> None:
    for service, state in (("local-llm", standard), ("flash-next", flash)):
        gateway.containers.append(
            ContainerInfo(
                service=service, state=state, health=None, started_at=None, image="i"
            )
        )


def test_the_up_predicate_counts_every_state_that_holds_memory() -> None:
    for state in ("running", "paused", "restarting"):
        assert engine_up(state)
    for state in ("exited", "created", "dead", "missing", ""):
        assert not engine_up(state)
    assert "restarting" in ENGINE_UP_STATES


# --- /restart -------------------------------------------------------------------


@pytest.mark.parametrize("state", ["exited", "created"])
def test_restart_refuses_a_stopped_engine(
    client: TestClient, gateway: FakeGateway, state: str
) -> None:
    """The created-and-stopped engine is exactly what Ops "Restart flash-next" would
    otherwise start beside the running one."""
    _engines(gateway, "running", state)

    resp = client.post("/restart", json={"service": "flash-next"}, headers=AUTH)

    assert resp.status_code == 409
    assert "not running" in resp.json()["detail"]
    assert gateway.restarted == []


def test_restart_of_the_running_engine_passes(
    client: TestClient, gateway: FakeGateway
) -> None:
    _engines(gateway, "running", "exited")

    resp = client.post("/restart", json={"service": "local-llm"}, headers=AUTH)

    assert resp.status_code == 202
    assert gateway.restarted == ["local-llm"]


def test_restart_of_a_running_engine_goes_through_the_guard(
    client: TestClient, gateway: FakeGateway
) -> None:
    # Both up is already the §4d violation; a restart must not be a way to re-assert it.
    _engines(gateway, "running", "restarting")

    resp = client.post("/restart", json={"service": "local-llm"}, headers=AUTH)

    assert resp.status_code == 409
    assert "flash-next is restarting" in resp.json()["detail"]
    assert gateway.restarted == []


def test_restart_of_an_engine_is_refused_during_a_oneshot(
    client: TestClient, gateway: FakeGateway
) -> None:
    _engines(gateway, "running", "exited")
    gateway.oneshot_running = "refresh"

    resp = client.post("/restart", json={"service": "local-llm"}, headers=AUTH)

    assert resp.status_code == 409
    assert gateway.restarted == []


def test_restart_all_skips_a_stopped_engine(
    client: TestClient, gateway: FakeGateway
) -> None:
    _engines(gateway, "running", "exited")

    resp = client.post("/restart", json={"service": "all"}, headers=AUTH)

    assert resp.status_code == 202
    assert "flash-next" not in gateway.restarted
    assert "local-llm" in gateway.restarted
    assert resp.json()["restarting"] == [
        "api",
        "local-llm",
        "postgres",
        "supervisor",
    ]


def test_restart_all_skips_engines_the_guard_refuses(
    client: TestClient, gateway: FakeGateway
) -> None:
    _engines(gateway, "running", "exited")
    gateway.oneshot_running = "update"

    resp = client.post("/restart", json={"service": "all"}, headers=AUTH)

    assert resp.status_code == 202
    assert "local-llm" not in gateway.restarted
    assert "api" in gateway.restarted


# --- /start ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["update", "refresh", "rebuild", "provision", "perplexity", "export"]
)
def test_start_refuses_an_engine_while_any_oneshot_runs(
    client: TestClient, gateway: FakeGateway, kind: str
) -> None:
    """An update/refresh/rebuild/provision drives the engines with docker directly; a
    start from here would race it, not only a perplexity run."""
    _engines(gateway, "exited", "exited")
    if kind == "update":
        gateway.updater_running = True
    else:
        gateway.oneshot_running = kind

    resp = client.post("/start", json={"service": "flash-next"}, headers=AUTH)

    assert resp.status_code == 409
    assert kind in resp.json()["detail"]
    assert gateway.started == []


@pytest.mark.parametrize("state", ["paused", "restarting"])
def test_start_refuses_while_the_other_engine_holds_memory(
    client: TestClient, gateway: FakeGateway, state: str
) -> None:
    """A crash-looping (restarting) engine re-allocates on every loop: it must be
    STOPPED before the other starts, never treated as down."""
    _engines(gateway, state, "exited")

    resp = client.post("/start", json={"service": "flash-next"}, headers=AUTH)

    assert resp.status_code == 409
    assert gateway.started == []


class _SlowStartGateway(FakeGateway):
    """A start that takes a moment to show as running — the window in which an
    unlocked guard lets a second start through."""

    def start(self, service: str) -> None:
        super().start(service)
        time.sleep(0.2)
        self.containers = [
            replace(c, state="running") if c.service == service else c
            for c in self.containers
        ]


def test_concurrent_engine_starts_put_up_only_one() -> None:
    gateway = _SlowStartGateway(containers=[])
    _engines(gateway, "exited", "exited")
    app = create_app(Settings(supervisor_token=TOKEN), gateway, watch_api=False)
    barrier = threading.Barrier(2)

    def call(service: str) -> int:
        with TestClient(app) as c:
            barrier.wait()
            return c.post("/start", json={"service": service}, headers=AUTH).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, s) for s in ("local-llm", "flash-next")]
        codes = sorted(f.result() for f in futures)

    assert codes == [202, 409], codes
    assert len(gateway.started) == 1, gateway.started
