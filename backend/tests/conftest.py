import os
import shutil
import subprocess
from typing import TYPE_CHECKING

# Off for the WHOLE suite, before any Settings is constructed. The vitals sampler writes a
# reading into the ring once a second for the life of the process, so any test that seeds
# the ring and then asserts on it is racing a tick that overwrites the seed with whatever an
# absent gauge reads (None). Set here rather than in 64 individual Settings(...) calls, and
# as an env var because that is what Settings reads.
os.environ.setdefault("JBRAIN_VITALS_SAMPLER_ENABLED", "false")


if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer


def docker_available() -> bool:
    """Integration tests need a daemon; Claude Code web sessions lack one."""
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def _free_host_postgres_port() -> None:
    """Remove any project container still holding host port 5432.

    Under host networking the container's port IS the host port, unmapped — so Postgres on
    5432 is a process-wide singleton for the whole box. A container left behind by an
    interrupted run makes the next one fail to bind and exit(1), while the admin connection
    to 127.0.0.1:5432 quietly lands on the SURVIVOR — which already has the `jbrain_app`
    role, so provisioning dies with a baffling "role already exists" that has nothing to do
    with the code under test. (This cost a full suite run twice before it was understood.)

    Only ever removes containers of THIS image, so it cannot touch a developer's own
    Postgres. Best-effort: on a normal bridged daemon this never runs at all."""
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "ancestor=timescale/timescaledb-ha:pg17"],
        capture_output=True,
        text=True,
        check=False,
    )
    stale = [line for line in result.stdout.split() if line]
    if stale:
        subprocess.run(["docker", "rm", "-f", *stale], capture_output=True, check=False)


def pgvector_container() -> "PostgresContainer":
    """The production Postgres image, which also runs on bridge-less daemons.

    Uses `timescale/timescaledb-ha:pg17` — the same image production runs — so the
    integration suite exercises the real engine set: pgvector (migration 0003),
    plus TimescaleDB hypertables and PostGIS (Phase 7 location). The plain alpine
    or pgvector-only images ship none of the latter, so the location migrations
    would fail to even apply.

    Sandboxed dev environments run dockerd with --bridge=none --iptables=false,
    so published ports never materialize and the Ryuk reaper cannot start.
    There we fall back to host networking and talk to Postgres on its
    in-container port directly; CI and normal daemons keep the standard
    mapped-port path.
    """
    from testcontainers.postgres import PostgresContainer

    image = "timescale/timescaledb-ha:pg17"
    has_bridge = (
        subprocess.run(
            ["docker", "network", "inspect", "bridge"], capture_output=True, check=False
        ).returncode
        == 0
    )
    if has_bridge:
        return PostgresContainer(image)

    # Ryuk needs a published port; leak protection is moot in a throwaway
    # sandbox. The context manager still stops the container on exit.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

    class HostNetworkPostgres(PostgresContainer):
        def get_container_host_ip(self) -> str:
            return "127.0.0.1"

        def get_exposed_port(self, port: int) -> int:
            # Host networking: container ports ARE host ports, unmapped.
            return int(port)

    container = HostNetworkPostgres(image).with_kwargs(network_mode="host")
    # Port bindings are rejected outright under host networking.
    container.ports.clear()
    _free_host_postgres_port()
    return container
