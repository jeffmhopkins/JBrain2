"""Code-mode (jcode) deploy guarantees, asserted on the compose file.

The api PROXIES the owner's code-mode control surface to the jcode control server
at http://jcode:9100. That service lives ONLY on the fenced `jcode_fenced` network
(kept off `internal` so the arbitrary-code sandbox can't reach db/worker/blobs), so the api
must ALSO join `jcode_fenced` — otherwise it can't resolve/reach the control server and
every /api/jcode/* create 502s. This regression-guards exactly that wiring, which was missing
from the original Wave J2 compose."""

import re
from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"


def _spec() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def test_api_joins_the_jcode_network() -> None:
    # Without this the api cannot reach jcode:9100 and code-mode create 502s.
    api = _spec()["services"]["api"]["networks"]
    assert "jcode_fenced" in api, (
        "the api proxies http://jcode:9100, which is only on the `jcode_fenced` network — "
        "the api must join it or every /api/jcode/* call 502s"
    )


def test_jcode_is_isolated_on_its_fenced_network_only() -> None:
    # The sandbox stays OFF `internal`: it must not reach db/worker/blobs.
    services = _spec()["services"]
    assert services["jcode"]["networks"] == ["jcode_fenced"]
    # Profile-gated so a stock deploy never starts the sandbox.
    assert services["jcode"]["profiles"] == ["jcode"]


def test_jcode_sessions_cannot_regain_privileges() -> None:
    # Session shells are unprivileged uids; no setuid/file-capability binary may lift one.
    assert "no-new-privileges:true" in _spec()["services"]["jcode"]["security_opt"]


def test_jcode_pids_limit_covers_every_session_at_its_cap() -> None:
    # Per-uid RLIMIT_NPROC bounds one session; the container's pids_limit must hold all
    # of them at once plus the root server's own threads and git runs.
    ctl = Path(__file__).resolve().parents[3] / "jcode" / "src" / "jcode_ctl"
    nproc_m = re.search(r"^SESSION_NPROC = (\d+)", (ctl / "sandbox.py").read_text(), re.M)
    max_m = re.search(r"max_sessions: int = (\d+)", (ctl / "config.py").read_text())
    limit_m = re.search(r"\$\{JCODE_PIDS_LIMIT:-(\d+)\}", _COMPOSE.read_text())
    assert nproc_m and max_m and limit_m
    sessions = int(max_m.group(1)) * int(nproc_m.group(1))
    assert int(limit_m.group(1)) >= sessions + 256


def test_jcode_reaps_orphaned_session_processes() -> None:
    # Without an init, uvicorn is PID 1 and orphans become zombies counted in pids_limit.
    assert _spec()["services"]["jcode"]["init"] is True


def test_jcode_marks_itself_a_sandbox_so_the_cli_runs_as_root() -> None:
    # Session shells now run as their own uid (security plan S2.1), so the CLI's root guard
    # no longer fires; the marker stays for any CLI behaviour keyed off it.
    env = _spec()["services"]["jcode"]["environment"]
    assert env.get("IS_SANDBOX") == "1", "the jcode container must keep IS_SANDBOX=1"


# --- Wave J5 hardening: the dataless-sandbox boundary, asserted on the compose file ---


def test_jcode_mounts_no_docker_socket() -> None:
    # The single worst escape: the Docker socket would let arbitrary agent/shell code
    # spawn a privileged sibling and own the host. The sandbox must never mount it.
    vols = _spec()["services"]["jcode"].get("volumes", [])
    assert all("docker.sock" not in str(v) for v in vols), (
        "jcode must not mount the Docker socket — it would hand the sandbox host root"
    )


def test_jcode_mounts_only_its_own_scratch_volume() -> None:
    # The only state the sandbox touches is its per-session checkouts on jcode_work. No
    # host bind, no blob/notes/db path — guard that the mount set stays exactly that.
    vols = _spec()["services"]["jcode"].get("volumes", [])
    assert vols == ["jcode_work:/work"], (
        "jcode must mount ONLY its scratch volume — any extra mount risks exposing host "
        f"or owner data to the sandbox; got {vols!r}"
    )


def test_jcode_preview_defaults_on() -> None:
    # Preview rides the opt-in jcode profile: enabling code mode already enables preview,
    # with no second env var to set. Guard that a compose edit can't silently flip the
    # default back to off and reintroduce the extra opt-in step.
    env = _spec()["services"]["jcode"]["environment"]
    assert env.get("JCODE_PREVIEW_ENABLED") == "${JCODE_PREVIEW_ENABLED:-true}", (
        "preview must default ON with code mode — the profile is already opt-in; an "
        "owner shouldn't need a second env var to open a per-session preview"
    )


def test_jcode_declares_its_aggregate_resource_ceilings() -> None:
    # The aggregate caps are the hard ceiling on the shared sandbox container; the in-
    # server concurrency + disk limits govern per session. Guard that a compose edit can't
    # silently uncap CPU/memory/PIDs and let a runaway session starve the box.
    svc = _spec()["services"]["jcode"]
    assert "mem_limit" in svc and "cpus" in svc and "pids_limit" in svc, (
        "jcode must keep its mem_limit/cpus/pids_limit caps — they bound an "
        "arbitrary-code sandbox from starving the box"
    )


def test_flash_next_answers_at_the_gateway_name_its_clients_already_use() -> None:
    """The api reaches its model at http://local-llm:8080. Flash-Next carries that alias on the
    same network, so a switch needs no client change — and, like local-llm, it is on no
    network the sandbox shares (N1: jcode's models come through the api's residency-aware
    proxy, never the gateway directly)."""
    nets = _spec()["services"]["flash-next"]["networks"]
    assert set(nets) == set(_spec()["services"]["local-llm"]["networks"]) == {"internal"}
    assert nets["internal"]["aliases"] == ["local-llm"]
