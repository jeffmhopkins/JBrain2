"""The allowlisted perplexity one-shot (FLASH_NEXT_ENGINE_PLAN F2, check 6).

A FIXED job: the binary, the text file and the flags are constants; a request names only
a model path (validated to a .gguf under /models) and a bounded chunk count. These cover
the HTTP validation, the mutual exclusion it shares with every other one-shot, and the
script itself — run for real under `sh` against a fake `docker` on PATH, so the
stop-then-restore of the engine is exercised, not just pattern-matched.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from supervisor import gateway as gw
from supervisor import watchdog
from supervisor.gateway import (
    COMPOSE_ONEOFF_LABEL,
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    ComposeDockerGateway,
    ContainerInfo,
    UnknownServiceError,
    UpdateStatus,
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
    script = gw._perplexity_command("jbrain", MODEL, 25, "")

    assert "--entrypoint llama-perplexity" in script
    assert "run --rm --no-deps -T" in script
    assert f"-f {gw.PERPLEXITY_TEXT}" in script
    assert "-ngl 999 -ot per_layer_token_embd=CPU -c 512 --chunks 25" in script


def test_the_command_defaults_to_a_bounded_chunk_count() -> None:
    """Never the whole split: that is hours with the box held."""
    script = gw._perplexity_command("jbrain", MODEL, None, "")
    assert f"--chunks {gw.PERPLEXITY_DEFAULT_CHUNKS}" in script


def test_the_command_refuses_a_restore_target_that_is_not_an_engine() -> None:
    with pytest.raises(ValueError):
        gw._perplexity_command("jbrain", MODEL, None, "api; rm -rf /")


# A `docker` stand-in with STATE: a file per running engine under $STATE, so a stop
# really stops and the script's re-check sees it. STOP_FAIL makes `stop` fail;
# STOP_NOOP makes it "succeed" without stopping. While `compose` runs it logs which
# engines are up, which is what the one-engine assertions read.
_FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$LOG"
case "$1" in
  ps)
    if [ "$2" = -aq ]; then pool="local-llm flash-next"; else pool=$(ls "$STATE"); fi
    for s in $pool; do
      case "$*" in *"service=$s"*) echo "id-$s";; esac
    done ;;
  stop)
    [ -n "${STOP_FAIL:-}" ] && exit 1
    [ -n "${STOP_NOOP:-}" ] && exit 0
    shift 3
    for id in "$@"; do rm -f "$STATE/${id#id-}"; done ;;
  start)
    shift
    for id in "$@"; do touch "$STATE/${id#id-}"; done ;;
  compose)
    echo "compose-with-up:[$(ls "$STATE" | tr '\\n' ' ')]" >> "$LOG"
    exit "$PPL_RC" ;;
esac
exit 0
"""


def _run_script(
    tmp_path: Path,
    running: str,
    *,
    restore: str = "",
    ppl_rc: int = 0,
    model: str = MODEL,
    **env_extra: str,
) -> tuple[int, list[str], str]:
    log = tmp_path / "docker.log"
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    for svc in running.split():
        (state / svc).touch()
    fake = tmp_path / "docker"
    fake.write_text(_FAKE_DOCKER)
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "LOG": str(log),
        "STATE": str(state),
        "PPL_RC": str(ppl_rc),
        **env_extra,
    }
    proc = subprocess.run(
        ["sh", "-c", gw._perplexity_command("jbrain", model, 10, restore)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, calls, proc.stdout


def test_the_script_stops_the_running_engine_first_and_restarts_it(
    tmp_path: Path,
) -> None:
    rc, calls, out = _run_script(tmp_path, "flash-next", restore="flash-next")

    assert rc == 0, out
    acts = ("stop", "compose", "start")
    verbs = [c.split()[0] for c in calls if c.split()[0] in acts]
    assert verbs == ["stop", "compose", "start"], calls
    assert "compose-with-up:[]" in calls
    assert any(c.startswith("start id-flash-next") for c in calls)
    # Every lookup is scoped to the project's long-running containers, never a one-off.
    for c in (c for c in calls if c.startswith("ps")):
        assert f"label={COMPOSE_PROJECT_LABEL}=jbrain" in c
        assert f"label={COMPOSE_ONEOFF_LABEL}=False" in c
    assert "restarting flash-next" in out


def test_the_script_restores_the_engine_even_when_the_run_fails(
    tmp_path: Path,
) -> None:
    rc, calls, out = _run_script(tmp_path, "local-llm", restore="local-llm", ppl_rc=3)

    assert rc == 3
    assert any(c.startswith("start id-local-llm") for c in calls)
    assert "exit 3" in out


def test_the_script_restores_nothing_it_was_not_told_to(tmp_path: Path) -> None:
    """Two engines up (the §4d violation) reaches the script as restore="": both are
    stopped and neither is put back."""
    rc, calls, _ = _run_script(tmp_path, "local-llm flash-next", restore="")

    assert rc == 0
    assert "compose-with-up:[]" in calls
    assert not any(c.startswith("start") for c in calls)


def test_the_script_fails_closed_when_a_stop_fails(tmp_path: Path) -> None:
    rc, calls, out = _run_script(
        tmp_path, "local-llm", restore="local-llm", STOP_FAIL="1"
    )

    assert rc != 0
    assert not any(c.startswith("compose") for c in calls)
    assert "could not stop local-llm" in out


def test_the_script_fails_closed_when_an_engine_survives_its_stop(
    tmp_path: Path,
) -> None:
    rc, calls, out = _run_script(
        tmp_path, "flash-next", restore="flash-next", STOP_NOOP="1"
    )

    assert rc != 0
    assert not any(c.startswith("compose") for c in calls)
    assert "still running" in out


@pytest.mark.parametrize(
    "evil",
    [
        "/models/x/a.gguf'$(touch {mark})'",
        '/models/x/a.gguf"$(touch {mark})"',
        "/models/x/a.gguf`touch {mark}`",
        "/models/x/a.gguf'; touch {mark}; '",
        "/models/x/a.gguf'\"'\"'$(touch {mark})",
    ],
)
def test_a_hostile_path_runs_nothing_anywhere_in_the_script(
    tmp_path: Path, evil: str
) -> None:
    """The whole generated script, run for real: the HTTP layer would refuse these, and
    the script must still not execute them if a caller ever skips that check."""
    mark = tmp_path / "pwned"
    rc, calls, _ = _run_script(tmp_path, "", model=evil.format(mark=mark))

    assert rc == 0
    assert not mark.exists()
    assert any(c.startswith("compose") for c in calls)


# --- the gateway: labels, orphans, the reaper ---------------------------------------


class _Container:
    def __init__(
        self,
        service: str = "",
        *,
        oneoff: bool = False,
        running: bool = True,
        labels: dict[str, str] | None = None,
        name: str = "",
    ) -> None:
        self.name = name
        self.labels = labels or {
            COMPOSE_PROJECT_LABEL: "jbrain",
            COMPOSE_SERVICE_LABEL: service,
            COMPOSE_ONEOFF_LABEL: "True" if oneoff else "False",
        }
        self.attrs: dict[str, Any] = {
            "State": {"Status": "running" if running else "exited", "Running": running},
            "Config": {"Image": "img"},
            "Created": "2026-10-01T00:00:00Z",
        }
        self.started = False
        self.removed = False

    def start(self) -> None:
        self.started = True
        self.attrs["State"].update(Status="running", Running=True)

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _NotFound(Exception):
    pass


_NotFound.__name__ = "NotFound"


class _Containers:
    def __init__(self, items: list[_Container]) -> None:
        self._items = items
        self.runs: list[dict[str, Any]] = []
        self.orphan: _Container | None = None

    def run(self, image: str, **kwargs: Any) -> None:
        self.runs.append(kwargs)

    def get(self, name: str) -> _Container:
        if name == gw.PERPLEXITY_CONTAINER and self.orphan and not self.orphan.removed:
            return self.orphan
        raise _NotFound(name)

    def list(self, all: bool = False, filters: dict[str, Any] | None = None) -> list:
        raw = (filters or {}).get("label", [])
        wanted = [raw] if isinstance(raw, str) else list(raw)

        def ok(c: _Container) -> bool:
            for w in wanted:
                key, sep, value = str(w).partition("=")
                if key not in c.labels or (sep and c.labels[key] != value):
                    return False
            return not c.removed

        return [c for c in self._items if ok(c)]


def _compose(items: list[_Container]) -> tuple[ComposeDockerGateway, _Containers]:
    containers = _Containers(items)
    client = type("C", (), {"containers": containers})()
    return ComposeDockerGateway(cast(Any, client), "jbrain", "/opt/jbrain2"), containers


def test_a_running_oneoff_never_stands_in_for_its_service() -> None:
    """`compose run flash-next` carries the service label too. A start aimed at the
    engine must reach the real (stopped) container, not the perplexity run."""
    real = _Container("flash-next", running=False)
    run = _Container("flash-next", oneoff=True)
    gateway, _ = _compose([run, real])

    gateway.start("flash-next")

    assert real.started and not run.started
    assert [(c.service, c.state) for c in gateway.list_containers()] == [
        ("flash-next", "running")
    ]
    # ...but it is not hidden either: it is listed as what it is.
    assert [c.service for c in gateway.list_oneoffs()] == ["flash-next"]


def test_a_service_with_only_a_oneoff_is_not_provisioned() -> None:
    gateway, _ = _compose([_Container("flash-next", oneoff=True)])

    with pytest.raises(UnknownServiceError):
        gateway.start("flash-next")


def test_the_gateway_labels_the_engine_to_restore() -> None:
    gateway, containers = _compose(
        [_Container("local-llm"), _Container("flash-next", running=False)]
    )

    name = gateway.start_perplexity(MODEL, 5)

    assert name.startswith("jbrain-perplexity-")
    (run,) = containers.runs
    assert run["labels"] == {
        gw.ONESHOT_LABEL: "perplexity",
        gw.PERPLEXITY_RESTORE_LABEL: "local-llm",
    }
    assert run["command"][-1] == gw._perplexity_command("jbrain", MODEL, 5, "local-llm")


def test_two_engines_up_restore_neither() -> None:
    gateway, containers = _compose([_Container("local-llm"), _Container("flash-next")])
    gateway.start_perplexity(MODEL, 5)
    assert containers.runs[0]["labels"][gw.PERPLEXITY_RESTORE_LABEL] == ""


def _hung_perplexity(restore: str) -> _Container:
    epoch = int(time.time()) - gw._ONESHOT_MAX_RUNTIME_S - 60
    return _Container(
        labels={gw.ONESHOT_LABEL: "perplexity", gw.PERPLEXITY_RESTORE_LABEL: restore},
        name=f"jbrain-perplexity-{epoch}",
    )


def test_reaping_a_hung_run_removes_its_model_and_restores_the_engine() -> None:
    """The reaper SIGKILLs the one-shot, so its EXIT trap never runs. Without this the
    ~60 GiB model container kept running and the engine stayed down."""
    hung = _hung_perplexity("local-llm")
    engine = _Container("local-llm", running=False)
    gateway, containers = _compose(
        [hung, engine, _Container("flash-next", running=False)]
    )
    containers.orphan = _Container(name=gw.PERPLEXITY_CONTAINER)

    assert gateway.running_oneshot() is None

    assert hung.removed and containers.orphan.removed
    assert engine.started


def test_the_reaper_never_starts_an_engine_beside_another() -> None:
    hung = _hung_perplexity("local-llm")
    standard = _Container("local-llm", running=False)
    gateway, _ = _compose([hung, standard, _Container("flash-next")])

    gateway.running_oneshot()

    assert hung.removed and not standard.started


def test_the_reaper_restores_nothing_when_the_run_will_not_die() -> None:
    hung = _hung_perplexity("local-llm")
    standard = _Container("local-llm", running=False)
    gateway, containers = _compose([hung, standard])
    stuck = _Container(name=gw.PERPLEXITY_CONTAINER)

    def refuse(force: bool = False) -> None:
        raise RuntimeError("device busy")

    stuck.remove = refuse  # type: ignore[method-assign]
    containers.orphan = stuck

    gateway.running_oneshot()

    assert not standard.started


def test_any_oneshot_start_clears_an_orphaned_perplexity_model() -> None:
    """An update must never start engines beside a run nobody is tracking."""
    gateway, containers = _compose([])
    containers.orphan = _Container(name=gw.PERPLEXITY_CONTAINER)

    gateway.start_update()

    assert containers.orphan.removed
    assert len(containers.runs) == 1


def test_running_oneshot_names_the_kind() -> None:
    live = _Container(
        labels={gw.ONESHOT_LABEL: "refresh"}, name=f"jbrain-refresh-{int(time.time())}"
    )
    gateway, _ = _compose([live])
    assert gateway.running_oneshot() == "refresh"
    updater = _Container(
        labels={gw.UPDATER_LABEL: "1"}, name=f"jbrain-updater-{int(time.time())}"
    )
    gateway, _ = _compose([updater])
    assert gateway.running_oneshot() == "update"


# --- the one-engine guard on /start, /oneshot, /status, the watchdog -----------------


def _engines(gateway: FakeGateway, standard: str, flash: str) -> None:
    for service, state in (("local-llm", standard), ("flash-next", flash)):
        gateway.containers.append(
            ContainerInfo(
                service=service, state=state, health=None, started_at=None, image="i"
            )
        )


def test_start_refuses_an_engine_while_the_other_runs(
    client: TestClient, gateway: FakeGateway
) -> None:
    _engines(gateway, "running", "exited")

    resp = client.post("/start", json={"service": "flash-next"}, headers=AUTH)

    assert resp.status_code == 409 and "local-llm is running" in resp.json()["detail"]
    assert gateway.started == []


def test_start_refuses_an_engine_during_a_perplexity_run(
    client: TestClient, gateway: FakeGateway
) -> None:
    _engines(gateway, "exited", "exited")
    gateway.oneshot_running = "perplexity"

    resp = client.post("/start", json={"service": "local-llm"}, headers=AUTH)

    assert resp.status_code == 409
    assert gateway.started == []


def test_start_of_an_engine_with_the_other_down_and_non_engines_are_untouched(
    client: TestClient, gateway: FakeGateway
) -> None:
    _engines(gateway, "running", "exited")
    gateway.oneshot_running = "perplexity"
    # A non-engine is not this guard's business.
    assert (
        client.post("/start", json={"service": "api"}, headers=AUTH).status_code == 202
    )
    gateway.oneshot_running = None
    assert (
        client.post("/start", json={"service": "local-llm"}, headers=AUTH).status_code
        == 202
    )


def test_oneshot_reports_what_is_in_flight(
    client: TestClient, gateway: FakeGateway
) -> None:
    assert client.get("/oneshot").status_code == 401
    assert client.get("/oneshot", headers=AUTH).json() == {"running": None}
    gateway.oneshot_running = "refresh"
    assert client.get("/oneshot", headers=AUTH).json() == {"running": "refresh"}


def test_status_lists_oneoffs_apart(client: TestClient, gateway: FakeGateway) -> None:
    gateway.oneoffs.append(
        ContainerInfo(
            service="flash-next",
            state="running",
            health=None,
            started_at=None,
            image="i",
        )
    )
    body = client.get("/status", headers=AUTH).json()
    assert [c["service"] for c in body["oneoffs"]] == ["flash-next"]
    assert "flash-next" not in [c["service"] for c in body["containers"]]


@pytest.mark.parametrize("kind", ["perplexity", "refresh"])
def test_the_watchdog_treats_perplexity_and_refresh_as_busy(kind: str) -> None:
    class _Only:
        def update_status(self, tail: int) -> UpdateStatus:
            return UpdateStatus(state="none", exit_code=None, log_tail="")

        def oneshot_status(self, name: str, tail: int) -> UpdateStatus:
            state = "running" if name == kind else "none"
            return UpdateStatus(state=state, exit_code=None, log_tail="")

    assert watchdog._update_running(cast(Any, _Only())) is True
