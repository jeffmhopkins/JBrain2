"""Code-mode (jcode) deploy guarantees, asserted on the compose file.

The api PROXIES the owner's code-mode control surface to the jcode control server
at http://jcode:9100. That service lives ONLY on the isolated `jcode` network (kept
off `internal` so the arbitrary-code sandbox can't reach db/worker/blobs), so the api
must ALSO join `jcode` — otherwise it can't resolve/reach the control server and every
/api/jcode/* create 502s. This regression-guards exactly that wiring, which was missing
from the original Wave J2 compose."""

from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"


def _spec() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def test_api_joins_the_jcode_network() -> None:
    # Without this the api cannot reach jcode:9100 and code-mode create 502s.
    api = _spec()["services"]["api"]["networks"]
    assert "jcode" in api, (
        "the api proxies http://jcode:9100, which is only on the `jcode` network — "
        "the api must join it or every /api/jcode/* call 502s"
    )


def test_jcode_and_shim_are_isolated_on_the_jcode_network_only() -> None:
    # The sandbox + its shim stay OFF `internal`: they must not reach db/worker/blobs.
    services = _spec()["services"]
    assert services["jcode"]["networks"] == ["jcode"]
    assert services["claude-shim"]["networks"] == ["jcode"]
    # Both are profile-gated so a stock deploy never starts the sandbox.
    assert services["jcode"]["profiles"] == ["jcode"]
    assert services["claude-shim"]["profiles"] == ["jcode"]


def test_jcode_marks_itself_a_sandbox_so_the_cli_runs_as_root() -> None:
    # The container runs as root; the bundled CLI refuses --dangerously-skip-permissions
    # (our bypassPermissions mode) as root unless IS_SANDBOX=1. Without this every turn
    # exits 1 before any model call — guard the escape hatch the on-box bring-up needed.
    env = _spec()["services"]["jcode"]["environment"]
    assert env.get("IS_SANDBOX") == "1", (
        "jcode runs the Claude CLI as root with bypassPermissions — it needs IS_SANDBOX=1 "
        "or the CLI refuses to start and every turn exits 1"
    )


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


def test_jcode_declares_its_aggregate_resource_ceilings() -> None:
    # The aggregate caps are the hard ceiling on the shared sandbox container; the in-
    # server concurrency + disk limits govern per session. Guard that a compose edit can't
    # silently uncap CPU/memory/PIDs and let a runaway session starve the box.
    svc = _spec()["services"]["jcode"]
    assert "mem_limit" in svc and "cpus" in svc and "pids_limit" in svc, (
        "jcode must keep its mem_limit/cpus/pids_limit caps — they bound an "
        "arbitrary-code sandbox from starving the box"
    )
