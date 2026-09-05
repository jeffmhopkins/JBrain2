"""Pull main and rebuild ONE service: validation, trigger, conflict, and status.

The fast path between `rebuild` (applies code already on the box, never pulls) and
`update` (pulls and rebuilds the world, about ten minutes). It exists because the sdr
sidecar is pure Python behind an apt-only image, so a one-line change to a measurement
cost a full system update to try — on a box whose owner has no terminal to shortcut it
with (CLAUDE.md #10).
"""

import shlex

from fastapi.testclient import TestClient

from supervisor import gateway as gw
from tests.conftest import AUTH, FakeGateway


def test_refresh_requires_token(client: TestClient) -> None:
    assert client.post("/refresh", json={"service": "api"}).status_code == 401
    assert client.get("/refresh/status").status_code == 401


def test_refresh_triggers_a_oneshot_for_the_service(
    client: TestClient, gateway: FakeGateway
) -> None:
    resp = client.post("/refresh", json={"service": "api"}, headers=AUTH)

    assert resp.status_code == 202
    assert resp.json()["oneshot"].startswith("jbrain-refresh-")
    assert ("refresh", "api") in gateway.oneshots_started


def test_refresh_of_an_unknown_service_is_404(
    client: TestClient, gateway: FakeGateway
) -> None:
    """Validated against the LIVE service set, so only a real compose service reaches
    the shell-quoted command."""
    resp = client.post("/refresh", json={"service": "nope"}, headers=AUTH)

    assert resp.status_code == 404
    assert gateway.oneshots_started == []


def test_refresh_conflicts_with_a_running_oneshot(client: TestClient) -> None:
    """It pulls the shared source mirror, so it must not race an update over it."""
    assert (
        client.post("/refresh", json={"service": "api"}, headers=AUTH).status_code
        == 202
    )

    assert client.post("/update", headers=AUTH).status_code == 409
    assert (
        client.post(
            "/refresh", json={"service": "supervisor"}, headers=AUTH
        ).status_code
        == 409
    )


def test_refresh_status_lifecycle(client: TestClient, gateway: FakeGateway) -> None:
    assert client.get("/refresh/status", headers=AUTH).json() == {
        "state": "none",
        "exit_code": None,
        "log_tail": "",
    }

    client.post("/refresh", json={"service": "api"}, headers=AUTH)
    assert client.get("/refresh/status", headers=AUTH).json()["state"] == "running"

    gateway.oneshot_running = None
    done = client.get("/refresh/status", headers=AUTH).json()
    assert done["state"] == "exited"
    assert done["exit_code"] == 0


def test_the_refresh_command_installs_git_because_it_pulls() -> None:
    """The docker:cli one-shot image has no git. `update` installs it for the same
    reason; `rebuild` does not, because it never pulls — and this one does."""
    command = gw._refresh_command("sdr")

    assert "apk add --no-cache git" in command
    assert "src/deploy/refresh-inner.sh" in command


def test_the_refresh_command_shell_quotes_the_service() -> None:
    """Validated at the HTTP layer AND quoted here, so a future caller that forgets the
    first cannot turn a service name into a command."""
    evil = "sdr'; rm -rf /"

    assert shlex.quote(evil) in gw._refresh_command(evil)
