import os
import shutil
import subprocess
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.types import (
    DEFAULT_MAX_TOKENS,
    LlmImage,
    LlmResult,
    LlmUsage,
    Sampling,
    parse_json_payload,
)

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer


class SchemaRoutedLlmClient(FakeLlmClient):
    """A FakeLlmClient that answers by request SCHEMA, not call order.

    integrate_note now makes one note.extract call PER SOURCE (the note body plus
    each attachment — per-source extraction, so a rich attachment can't crowd the
    body's own facts out of a shared budget), so the note.extract call COUNT varies
    per note and a positional script desyncs. This keys on the schema instead: an
    intent-schema call (integrate.note) gets `intent`; every other call (note.extract,
    for however many source groups the note has) gets `extraction`. Order- and
    count-independent, so a test need not know how many sources a note carries."""

    def __init__(self, extraction: str, intent: str) -> None:
        super().__init__([extraction])
        self._extraction = extraction
        self._intent = intent

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        images: Sequence[LlmImage] = (),
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        reasoning_effort: str | None = None,
        sampling: Sampling | None = None,
    ) -> LlmResult:
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user_text": user_text,
                "images": list(images),
                "json_schema": json_schema,
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
                "sampling": sampling,
            }
        )
        props = (json_schema or {}).get("properties", {})
        text = self._intent if "resolutions" in props else self._extraction
        parsed = parse_json_payload(text) if json_schema is not None else None
        return LlmResult(text=text, parsed=parsed, usage=LlmUsage(1, 1))


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
