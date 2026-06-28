"""Enabling host-mode jcode web preview without hand-editing .env. `jbrain
enable-jcode-preview [host]` (and the update self-heal) drive deploy/jcode-preview-setup.sh,
which writes JCODE_PREVIEW_MODE=host + JCODE_PREVIEW_BASE_HOST into .env, defaulting the
base host to JBRAIN_DOMAIN. It refuses on a box that can't serve previews (no jcode / no
tunnel) and fails closed on a malformed host, so a bad value never reaches the rendered
Caddy site. See docs/JCODE_PREVIEW_HOST_PLAN.md."""

import subprocess
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_SETUP = _DEPLOY / "jcode-preview-setup.sh"
_BACKFILL = _DEPLOY / "jcode-preview-backfill.sh"
_JBRAIN = _DEPLOY / "jbrain"
_UPDATE_INNER = _DEPLOY / "update-inner.sh"

# A box that CAN serve host preview: jcode on, tunnel mode on, a domain to default to.
_READY_ENV = (
    "JBRAIN_DOMAIN=box.example\n"
    "JBRAIN_SITE_ADDR=http://box.example\n"
    "TUNNEL_ENABLED=true\n"
    "JCODE_ENABLED=true\n"
    "JCODE_TOKEN=deadbeef\n"
)


def _run(env_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_SETUP), str(env_file), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _keys(env_file: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_enables_host_mode_defaulting_the_base_host_to_the_domain(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(_READY_ENV)
    assert _run(env).returncode == 0
    keys = _keys(env)
    assert keys["JCODE_PREVIEW_MODE"] == "host"
    # No arg -> the base host comes from JBRAIN_DOMAIN.
    assert keys["JCODE_PREVIEW_BASE_HOST"] == "box.example"


def test_an_explicit_base_host_overrides_the_domain(tmp_path: Path) -> None:
    # A subdomain deploy passes its zone apex explicitly (free SSL needs the apex).
    env = tmp_path / ".env"
    env.write_text(_READY_ENV)
    assert _run(env, "apex.example").returncode == 0
    assert _keys(env)["JCODE_PREVIEW_BASE_HOST"] == "apex.example"


def test_rerunning_replaces_in_place_without_stacking_duplicates(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(_READY_ENV)
    assert _run(env, "first.example").returncode == 0
    assert _run(env, "second.example").returncode == 0
    body = env.read_text()
    # Exactly one of each key, carrying the latest value.
    assert body.count("JCODE_PREVIEW_BASE_HOST=") == 1
    assert body.count("JCODE_PREVIEW_MODE=") == 1
    assert _keys(env)["JCODE_PREVIEW_BASE_HOST"] == "second.example"


def test_refuses_when_jcode_is_not_enabled(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("JBRAIN_DOMAIN=box.example\nTUNNEL_ENABLED=true\n")
    result = _run(env)
    assert result.returncode != 0
    # Fails closed: nothing written, so a stock stack can't drift into host mode.
    assert "JCODE_PREVIEW_MODE" not in env.read_text()


def test_refuses_when_not_in_tunnel_mode(tmp_path: Path) -> None:
    # Host preview rides the Cloudflare tunnel; direct/auto-TLS mode can't serve the
    # plain-http wildcard site.
    env = tmp_path / ".env"
    env.write_text("JBRAIN_DOMAIN=box.example\nJCODE_ENABLED=true\n")
    assert _run(env).returncode != 0
    assert "JCODE_PREVIEW_MODE" not in env.read_text()


def test_fails_closed_on_a_malformed_base_host(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(_READY_ENV)
    # A space, or Caddy's {placeholder} braces, would emit an invalid site address and
    # crash-loop the proxy — reject before it's written.
    for bad in ("bad host.example", "{placeholder}.example"):
        env.write_text(_READY_ENV)
        assert _run(env, bad).returncode != 0
        assert "JCODE_PREVIEW_MODE" not in env.read_text()


# --- the update-path self-heal (deploy/jcode-preview-backfill.sh) ---


def _backfill(env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_BACKFILL), str(env_file)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_backfill_fills_the_base_host_from_the_domain_once(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    # Host mode enabled but the base host went missing (an update of an older enable).
    env.write_text("JBRAIN_DOMAIN=box.example\nJCODE_PREVIEW_MODE=host\n")
    assert _backfill(env).returncode == 0
    assert _keys(env)["JCODE_PREVIEW_BASE_HOST"] == "box.example"
    # Re-running (every update) must not stack a second line.
    assert _backfill(env).returncode == 0
    assert env.read_text().count("JCODE_PREVIEW_BASE_HOST=") == 1


def test_backfill_is_a_noop_without_host_mode(tmp_path: Path) -> None:
    # A tunnel-mode / stock box is never flipped to host mode by an update.
    env = tmp_path / ".env"
    env.write_text("JBRAIN_DOMAIN=box.example\n")
    assert _backfill(env).returncode == 0
    assert "JCODE_PREVIEW_BASE_HOST" not in env.read_text()


def test_backfill_leaves_an_existing_base_host_untouched(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "JBRAIN_DOMAIN=box.example\nJCODE_PREVIEW_MODE=host\nJCODE_PREVIEW_BASE_HOST=apex.example\n"
    )
    assert _backfill(env).returncode == 0
    assert _keys(env)["JCODE_PREVIEW_BASE_HOST"] == "apex.example"


def test_backfill_never_stacks_empty_keys_when_the_domain_is_empty(tmp_path: Path) -> None:
    # The MAJOR the review caught: an empty/missing JBRAIN_DOMAIN must not append a bare
    # JCODE_PREVIEW_BASE_HOST= that re-stacks on every future update.
    env = tmp_path / ".env"
    env.write_text("JBRAIN_DOMAIN=\nJCODE_PREVIEW_MODE=host\n")
    for _ in range(3):
        assert _backfill(env).returncode == 0
    assert "JCODE_PREVIEW_BASE_HOST=" not in env.read_text()


def test_backfill_skips_a_malformed_domain(tmp_path: Path) -> None:
    # Same charset guard as the setup script / Caddy renderer — a bad domain isn't
    # copied verbatim to the edge.
    env = tmp_path / ".env"
    env.write_text("JBRAIN_DOMAIN={bad}.example\nJCODE_PREVIEW_MODE=host\n")
    assert _backfill(env).returncode == 0
    assert "JCODE_PREVIEW_BASE_HOST=" not in env.read_text()


def test_jbrain_exposes_the_enable_command_and_update_keeps_it_turnkey() -> None:
    jbrain = _JBRAIN.read_text()
    # The operator-facing command, listed and dispatched.
    assert "enable-jcode-preview" in jbrain
    assert "jcode-preview-setup.sh" in jbrain
    # Both update paths call the backfill helper, so an enabled host preview survives
    # updates like the other jcode keys — without ever auto-enabling host mode.
    for script in (_JBRAIN, _UPDATE_INNER):
        assert "jcode-preview-backfill.sh" in script.read_text()
