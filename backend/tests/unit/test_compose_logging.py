"""Every container's log is bounded, and bounded from the PWA.

Docker's default json-file driver has no max-size and no max-file, so each service writes
an unbounded log under /var/lib/docker — the same filesystem as the database, the blob
store, the backups and ~91 GB of model weights. The stack has natural floods (llama-swap's
access log, jlaunch's multi-hour jobs, the wall's 1 Hz loop, jcode/jlaunch at DEBUG, which
is exactly when the owner is investigating something).

The delivery mechanism is the point of this file, not just the bound. This was first written
as a `log-driver` default in /etc/docker/daemon.json — a HOST file needing a root shell and a
daemon reload, applied by an installer the owner cannot run. The owner operates this box
remotely with no terminal (CLAUDE.md #10), so that version would have taken effect on a fresh
install and never on their machine. Compose applies `logging:` at container creation, which
every update already does, so the PWA's Update button delivers it.

A service added later that omits the anchor fails here.
"""

from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"


def _spec() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())


def test_every_service_bounds_its_logs() -> None:
    """No exemptions. A one-shot (`migrate`, `wipe`) writes little and costs nothing to
    bound; a chatty service is exactly the one that must not be missed."""
    unbounded = [name for name, spec in _spec()["services"].items() if "logging" not in spec]
    assert unbounded == [], (
        f"services with unbounded logs: {unbounded} — add `logging: *logbound` "
        "(the anchor above `services:`)"
    )


def test_the_bound_is_a_real_ceiling_on_total_bytes() -> None:
    """max-size alone only bounds ONE file; without max-file the driver keeps rotating and
    the total is still unbounded. Both, on every service."""
    for name, spec in _spec()["services"].items():
        options = spec["logging"]["options"]
        assert spec["logging"]["driver"] == "json-file", name
        assert options["max-size"] == "50m", name
        assert options["max-file"] == "3", name


def test_the_services_share_one_anchor_rather_than_copies() -> None:
    """One definition, so changing the ceiling is one edit and cannot drift between
    services. Asserted on the TEXT because YAML resolves anchors away on load."""
    text = _COMPOSE.read_text()
    assert "x-logging: &logbound" in text, "the shared anchor is gone"
    assert text.count("logging: *logbound") == len(_spec()["services"]), (
        "a service defines its own logging block instead of referencing the anchor"
    )


def test_no_host_daemon_json_is_required() -> None:
    """The regression this file exists to prevent: reintroducing a host-file route that the
    owner has no way to apply. If a future change needs daemon.json for something else, that
    is a decision to make deliberately — and to make PWA-operable — not to drift back into."""
    install = (_COMPOSE.parent / "install.sh").read_text()
    assert "daemon.json" not in install, (
        "install.sh writes /etc/docker/daemon.json again — a host file the owner cannot "
        "apply from the PWA (CLAUDE.md #10)"
    )
