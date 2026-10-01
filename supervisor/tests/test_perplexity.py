"""The allowlisted perplexity one-shot (FLASH_NEXT_ENGINE_PLAN F2, check 6).

A FIXED job: the binary, the text file and the flags are constants; a request names only
a model path (validated to a .gguf under /models) and a bounded chunk count. These cover
the HTTP validation, the mutual exclusion it shares with every other one-shot, and the
script itself — run for real under `sh` against a fake `docker` on PATH, so the
stop-then-restore of the engine is exercised, not just pattern-matched.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from supervisor import gateway as gw
from supervisor.gateway import (
    COMPOSE_ONEOFF_LABEL,
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    ComposeDockerGateway,
    ContainerInfo,
    UnknownServiceError,
)
from tests.conftest import AUTH, FakeGateway

MODEL = "/models/qwen3.8-flash-next/UD-IQ4_XS/Qwen3.8-Flash-Next-00001-of-00003.gguf"


def _provision_flash_next(gateway: FakeGateway, state: str = "exited") -> None:
    gateway.containers.append(
        ContainerInfo(
            service="flash-next",
            state=state,
            health=None,
            started_at=None,
            image="jbrain2-flash-next:local",
        )
    )


def test_perplexity_requires_token(client: TestClient) -> None:
    assert client.post("/perplexity", json={"model_path": MODEL}).status_code == 401
    assert client.get("/perplexity/status").status_code == 401


def test_perplexity_starts_the_fixed_oneshot(
    client: TestClient, gateway: FakeGateway
) -> None:
    _provision_flash_next(gateway)

    resp = client.post(
        "/perplexity", json={"model_path": MODEL, "chunks": 40}, headers=AUTH
    )

    assert resp.status_code == 202
    assert resp.json()["oneshot"].startswith("jbrain-perplexity-")
    assert gateway.oneshots_started == [("perplexity", f"{MODEL}|40")]


def test_perplexity_without_the_service_is_404(
    client: TestClient, gateway: FakeGateway
) -> None:
    """The service is the image the job runs; a box that never provisioned it must not
    have compose build one on the spot."""
    resp = client.post("/perplexity", json={"model_path": MODEL}, headers=AUTH)

    assert resp.status_code == 404
    assert gateway.oneshots_started == []


@pytest.mark.parametrize(
    "path",
    [
        "/models/../etc/passwd.gguf",
        "/models/x/../../etc/shadow.gguf",
        "/etc/passwd",
        "/models/x/model.bin",
        "/models/x/-rf.gguf",
        "/models/x/a b.gguf",
        "/models/x/a.gguf; rm -rf /",
        "/models/x/a.gguf --chunks 9999",
        "/models/x/y/z/deep.gguf",
        "/models/x/a.gguf\n",
        "models/x/a.gguf",
    ],
)
def test_perplexity_rejects_any_path_outside_the_pattern(
    client: TestClient, gateway: FakeGateway, path: str
) -> None:
    _provision_flash_next(gateway)

    resp = client.post("/perplexity", json={"model_path": path}, headers=AUTH)

    assert resp.status_code == 400
    assert gateway.oneshots_started == []


@pytest.mark.parametrize("chunks", [0, -1, 201, 10_000])
def test_perplexity_bounds_the_chunk_count(
    client: TestClient, gateway: FakeGateway, chunks: int
) -> None:
    _provision_flash_next(gateway)

    resp = client.post(
        "/perplexity", json={"model_path": MODEL, "chunks": chunks}, headers=AUTH
    )

    assert resp.status_code == 422
    assert gateway.oneshots_started == []


def test_perplexity_refuses_any_other_field(
    client: TestClient, gateway: FakeGateway
) -> None:
    """No free-form args: an extra field is a 422, never a silently dropped attempt."""
    _provision_flash_next(gateway)

    resp = client.post(
        "/perplexity",
        json={"model_path": MODEL, "args": ["--override-kv", "x"]},
        headers=AUTH,
    )

    assert resp.status_code == 422
    assert gateway.oneshots_started == []


def test_perplexity_is_mutually_exclusive_with_other_oneshots(
    client: TestClient, gateway: FakeGateway
) -> None:
    _provision_flash_next(gateway)
    assert client.post("/update", headers=AUTH).status_code == 202

    resp = client.post("/perplexity", json={"model_path": MODEL}, headers=AUTH)

    assert resp.status_code == 409
    gateway.updater_running = False
    assert (
        client.post("/perplexity", json={"model_path": MODEL}, headers=AUTH).status_code
        == 202
    )
    # And while it runs, an update cannot start underneath it.
    assert client.post("/update", headers=AUTH).status_code == 409


def test_perplexity_status_lifecycle(client: TestClient, gateway: FakeGateway) -> None:
    _provision_flash_next(gateway)
    assert client.get("/perplexity/status", headers=AUTH).json()["state"] == "none"

    client.post("/perplexity", json={"model_path": MODEL}, headers=AUTH)
    assert client.get("/perplexity/status", headers=AUTH).json()["state"] == "running"

    gateway.oneshot_running = None
    done = client.get("/perplexity/status", headers=AUTH).json()
    assert done["state"] == "exited" and done["exit_code"] == 0


# --- the script itself ------------------------------------------------------------


def test_the_command_runs_only_the_fixed_binary_file_and_flags() -> None:
    script = gw._perplexity_command("jbrain", MODEL, 25)

    assert "--entrypoint llama-perplexity" in script
    assert "run --rm --no-deps -T" in script
    assert f"-f {gw.PERPLEXITY_TEXT}" in script
    assert "-ngl 999 -ot per_layer_token_embd=CPU -c 512 --chunks 25" in script


def test_the_command_omits_chunks_when_none() -> None:
    assert "--chunks" not in gw._perplexity_command("jbrain", MODEL, None)


def test_the_command_shell_quotes_the_model_path() -> None:
    """Validated at the HTTP layer AND quoted here, so a caller that skips the first
    still cannot turn the path into a command."""
    evil = "/models/x/a.gguf'; touch /tmp/pwned; '"

    assert shlex.quote(evil) in gw._perplexity_command("jbrain", evil, None)


def _run_script(
    tmp_path: Path, running: str, *, ppl_rc: int = 0
) -> tuple[int, list[str], str]:
    """Run the real script under sh with a fake `docker` that logs its argv, reports
    `running` as the up services, and exits `ppl_rc` from the compose run."""
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$LOG"\n'
        'if [ "$1" = ps ]; then\n'
        "  for s in $RUNNING; do\n"
        '    case "$*" in *"service=$s"*) echo "id-$s";; esac\n'
        "  done\n"
        "fi\n"
        'if [ "$1" = compose ]; then exit "$PPL_RC"; fi\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "LOG": str(log),
        "RUNNING": running,
        "PPL_RC": str(ppl_rc),
    }
    proc = subprocess.run(
        ["sh", "-c", gw._perplexity_command("jbrain", MODEL, 10)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, calls, proc.stdout


def test_the_script_stops_the_running_engine_first_and_restarts_it(
    tmp_path: Path,
) -> None:
    rc, calls, out = _run_script(tmp_path, "flash-next")

    assert rc == 0
    acts = ("stop", "compose", "start")
    verbs = [c.split()[0] for c in calls if c.split()[0] in acts]
    assert verbs == ["stop", "compose", "start"], calls
    assert any(c.startswith("stop -t 30 id-flash-next") for c in calls)
    assert any(c.startswith("start id-flash-next") for c in calls)
    # Every lookup is scoped to the project's long-running containers, never a one-off.
    for c in (c for c in calls if c.startswith("ps")):
        assert f"label={COMPOSE_PROJECT_LABEL}=jbrain" in c
        assert f"label={COMPOSE_ONEOFF_LABEL}=False" in c
    assert "restarting flash-next" in out


def test_the_script_restores_the_engine_even_when_the_run_fails(
    tmp_path: Path,
) -> None:
    rc, calls, out = _run_script(tmp_path, "local-llm", ppl_rc=3)

    assert rc == 3
    assert any(c.startswith("start id-local-llm") for c in calls)
    assert "exit 3" in out


def test_the_script_leaves_both_engines_down_if_both_were_up(tmp_path: Path) -> None:
    """Two engines up at once is already the §4d violation; restarting both would
    recreate the freeze the job just ended."""
    rc, calls, out = _run_script(tmp_path, "local-llm flash-next")

    assert rc == 0
    assert not any(c.startswith("start") for c in calls)
    assert "leaving both stopped" in out


def test_the_script_starts_nothing_when_no_engine_was_up(tmp_path: Path) -> None:
    rc, calls, _ = _run_script(tmp_path, "")

    assert rc == 0
    assert not any(c.startswith(("start", "stop")) for c in calls)
    assert any(c.startswith("compose") for c in calls)


# --- one-off containers are not the service ----------------------------------------


class _Container:
    def __init__(self, service: str, *, oneoff: bool, running: bool = True) -> None:
        self.labels = {
            COMPOSE_PROJECT_LABEL: "jbrain",
            COMPOSE_SERVICE_LABEL: service,
            COMPOSE_ONEOFF_LABEL: "True" if oneoff else "False",
        }
        self.attrs: dict[str, Any] = {
            "State": {"Status": "running" if running else "exited"},
            "Config": {"Image": "img"},
        }
        self.oneoff = oneoff
        self.started = False

    def start(self) -> None:
        self.started = True


class _Containers:
    def __init__(self, items: list[_Container]) -> None:
        self._items = items
        self.runs: list[dict[str, Any]] = []

    def run(self, image: str, **kwargs: Any) -> None:
        self.runs.append(kwargs)

    def list(self, all: bool = False, filters: dict[str, Any] | None = None) -> list:
        raw = (filters or {}).get("label", [])
        wanted = [raw] if isinstance(raw, str) else list(raw)
        pairs = {k: v for k, _, v in (str(w).partition("=") for w in wanted)}
        return [c for c in self._items if pairs.items() <= c.labels.items()]


def _compose(items: list[_Container]) -> ComposeDockerGateway:
    client = type("C", (), {"containers": _Containers(items)})()
    return ComposeDockerGateway(cast(Any, client), "jbrain", "/opt/jbrain2")


def test_a_running_oneoff_never_stands_in_for_its_service() -> None:
    """`compose run flash-next` carries the service label too. A start aimed at the
    engine must reach the real (stopped) container, not the perplexity run."""
    real = _Container("flash-next", oneoff=False, running=False)
    run = _Container("flash-next", oneoff=True)
    gateway = _compose([run, real])

    gateway.start("flash-next")

    assert real.started and not run.started
    states = [(c.service, c.state) for c in gateway.list_containers()]
    assert states == [("flash-next", "exited")]


def test_a_service_with_only_a_oneoff_is_not_provisioned() -> None:
    gateway = _compose([_Container("flash-next", oneoff=True)])

    with pytest.raises(UnknownServiceError):
        gateway.start("flash-next")


def test_the_gateway_launches_it_as_a_labelled_oneshot() -> None:
    containers = _Containers([])
    client = type("C", (), {"containers": containers})()
    gateway = ComposeDockerGateway(cast(Any, client), "jbrain", "/opt/jbrain2")

    name = gateway.start_perplexity(MODEL, 5)

    assert name.startswith("jbrain-perplexity-")
    (run,) = containers.runs
    assert run["labels"] == {gw.ONESHOT_LABEL: "perplexity"}
    assert run["command"][-1] == gw._perplexity_command("jbrain", MODEL, 5)
