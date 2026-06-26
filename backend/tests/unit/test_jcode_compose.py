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
