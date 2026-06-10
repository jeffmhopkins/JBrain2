import os
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer


def docker_available() -> bool:
    """Integration tests need a daemon; Claude Code web sessions lack one."""
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def pgvector_container() -> "PostgresContainer":
    """Postgres-with-pgvector container that also runs on bridge-less daemons.

    Migration 0003 needs pgvector; the plain alpine image doesn't ship it
    (production uses timescaledb-ha, which does).

    Sandboxed dev environments run dockerd with --bridge=none --iptables=false,
    so published ports never materialize and the Ryuk reaper cannot start.
    There we fall back to host networking and talk to Postgres on its
    in-container port directly; CI and normal daemons keep the standard
    mapped-port path.
    """
    from testcontainers.postgres import PostgresContainer

    image = "pgvector/pgvector:pg16"
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
    return container
