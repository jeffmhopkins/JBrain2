"""Docker access boundary: nothing outside this module touches the docker SDK.

The gateway exposes a deliberately fixed command surface (list, restart,
logs, log stream) so the HTTP layer cannot grow into a shell passthrough,
and so tests can substitute a fake without a docker daemon.
"""

from __future__ import annotations

import contextlib
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

    import docker
    from docker.models.containers import Container

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
# "True" on a `docker compose run` container. It carries the SAME service label as the
# long-running container, so a lookup by service alone can land on a transient one-off
# (an update's `run --rm api`, the perplexity job's `run flash-next`) instead of the
# service the caller meant to start, stop or read.
COMPOSE_ONEOFF_LABEL = "com.docker.compose.oneoff"

# Updater one-shots are deliberately OUTSIDE the compose project label so
# stack-wide restarts never touch a running update.
UPDATER_LABEL = "jbrain.updater"
# Export/import one-shots share the updater's detached-container pattern but
# carry their kind as the label value so each has its own status lookup.
ONESHOT_LABEL = "jbrain.oneshot"
UPDATER_IMAGE = "docker:cli"
# The container has docker+compose; git arrives via apk (the update needs
# network for `git pull` anyway, so this adds no new failure class).
UPDATE_COMMAND = (
    "apk add --no-cache git >/dev/null 2>&1 && exec sh src/deploy/update-inner.sh"
)
EXPORT_COMMAND = "exec sh src/deploy/export-inner.sh"
# Reset lives here, not in the api: dropping and re-migrating the schema needs
# the superuser role, which RLS does not bind, so the api's least-privilege role
# cannot do it — only a supervisor one-shot running superuser psql + alembic can.
RESET_COMMAND = "exec sh src/deploy/reset-inner.sh"
# Provision runs ONLY the local-model weight sync (download + re-stamp llama-swap +
# restart the gateway) — the tail of an update, WITHOUT git pull or rebuild. It is
# how the PWA's "Download" action installs a model on demand, decoupled from a full
# system update. No `apk add git`: a download-only sync needs no git.
PROVISION_COMMAND = "exec sh src/deploy/local-models-sync.sh"


# Rebuild ONE service: `docker compose build <svc>` then `up -d <svc>` — a targeted
# subset of an update (no git pull, no backup, one service), so a code/Dockerfile change
# already on the box (e.g. a new baked tts-stt voice) lands without a full update.
# The service is shell-quoted here AND validated against the live container set at the
# HTTP layer, so it can't inject; no `apk add git` — a rebuild needs no git.
def _rebuild_command(service: str) -> str:
    return f"exec sh src/deploy/rebuild-inner.sh {shlex.quote(service)}"


# Pull main and rebuild ONE service: the fast path between `rebuild` (applies code
# already on the box, never pulls) and `update` (pulls and rebuilds the world, unloading
# every model on the way). It exists because the sdr sidecar is pure Python behind an
# apt-only image, so a one-line change to a measurement otherwise costs a whole system
# update to try — and the owner has no terminal to shortcut it with (CLAUDE.md #10).
#
# It takes NO REF, exactly as the update does not: the inner script resets to the
# tracked upstream, so a token can ask for what a merged PR already put on `main` and
# nothing else. Same shell-quoting and live-service validation at the HTTP layer.
#
# `apk add git` because this one PULLS — the docker:cli image has no git, which is why
# UPDATE_COMMAND does the same and why `rebuild`, which never pulls, does not.
def _refresh_command(service: str) -> str:
    return (
        "apk add --no-cache git >/dev/null 2>&1 && "
        f"exec sh src/deploy/refresh-inner.sh {shlex.quote(service)}"
    )


# The two on-box LLM engines (backend `jbrain.llm.engine.SERVICE`). Never both up: on a
# 128 GB box their footprints together are a freeze (FLASH_NEXT_ENGINE_PLAN §4d).
ENGINE_SERVICES = ("local-llm", "flash-next")
FLASH_NEXT_SERVICE = "flash-next"
# The WikiText-2 raw test split, baked into the flash-next image at this fixed path
# by deploy/Dockerfile.flash-next, and llama.cpp's perplexity tool on that image's PATH.
PERPLEXITY_TEXT = "/opt/jbrain/eval/wiki.test.raw"
PERPLEXITY_BINARY = "llama-perplexity"
# Fixed name for the job's model container, so a run killed mid-way (the one-shot
# reaped, the daemon restarted) leaves something the next run can find and remove by
# name rather than a second ~60 GiB process nobody is tracking.
PERPLEXITY_CONTAINER = "jbrain-flash-next-perplexity"
# The flags are FIXED here, never taken from a request: `-ngl 999` is the full offload
# the serving config uses (the shape llama.cpp #29028 crashed on); the engram table
# stays on the CPU because the 26.8 GiB tensor exceeds Vulkan's 4 GiB binding limit;
# `-c 512` is the context the published WikiText-2 references are measured at.
PERPLEXITY_ARGS = ("-ngl", "999", "-ot", "per_layer_token_embd=CPU", "-c", "512")


def _perplexity_command(project: str, model_path: str, chunks: int | None) -> str:
    """The perplexity one-shot's whole script — a FIXED job, never free-form exec.

    The only caller-derived tokens are the model path (validated at the HTTP layer
    against a strict pattern under /models) and an integer chunk count; both are
    shell-quoted here too, so a caller that skips the validation still cannot inject.

    It STOPS whichever engine is up first, and restarts exactly that one afterwards.
    An idle-looking gateway is not enough: the warm keeper or a queued ingest can load
    a model into it mid-run, and that model plus the ~60 GiB this run loads is the
    freeze. Only a stopped container is race-free. Two engines found up at once is
    already the §4d violation, so neither is restarted and the log says so."""
    q = shlex.quote
    scope = (
        f"--filter {q(f'label={COMPOSE_PROJECT_LABEL}={project}')} "
        f"--filter {q(f'label={COMPOSE_ONEOFF_LABEL}=False')}"
    )
    args = [*PERPLEXITY_ARGS, *(("--chunks", str(int(chunks))) if chunks else ())]
    run = " ".join(
        [
            "docker compose --profile",
            q(FLASH_NEXT_SERVICE),
            "run --rm --no-deps -T --name",
            q(PERPLEXITY_CONTAINER),
            "--entrypoint",
            q(PERPLEXITY_BINARY),
            q(FLASH_NEXT_SERVICE),
            "-m",
            q(model_path),
            "-f",
            q(PERPLEXITY_TEXT),
            *(q(a) for a in args),
        ]
    )
    engines = " ".join(q(s) for s in ENGINE_SERVICES)
    return f"""set -u
ids() {{ docker ps $1 {scope} --filter "label={COMPOSE_SERVICE_LABEL}=$2"; }}
stopped=""
for svc in {engines}; do
  running=$(ids -q "$svc")
  if [ -n "$running" ]; then
    echo "[perplexity] stopping $svc for the run"
    docker stop -t 30 $running >/dev/null
    stopped="$stopped $svc"
  fi
done
restore() {{
  set -- $stopped
  if [ "$#" -eq 1 ]; then
    echo "[perplexity] restarting $1"
    docker start $(ids -aq "$1") >/dev/null || echo "[perplexity] could not restart $1"
  elif [ "$#" -gt 1 ]; then
    echo "[perplexity] both engines were up before the run; leaving both stopped"
  fi
}}
trap restore EXIT
docker rm -f {q(PERPLEXITY_CONTAINER)} >/dev/null 2>&1 || true
echo "[perplexity] {q(model_path)} on {PERPLEXITY_TEXT}"
rc=0
{run} || rc=$?
echo "[perplexity] exit $rc"
exit $rc
"""


# Docker reports this zero-value timestamp for containers that never started.
_NEVER_STARTED = "0001-01-01T00:00:00Z"

# A one-shot (update/provision/export/…) is a detached container expected to exit on
# its own. If one HANGS — e.g. a provision stuck on a silently stalled hf download —
# the mutual-exclusion guard would otherwise block every future update and provision
# FOREVER, since the wedged container never leaves the Running state. Past this age a
# still-running one-shot is treated as dead and reaped so a fresh one can start. Set
# well above any legitimate run (a slow multi-model download can be hours), so it only
# ever fires on a genuine wedge, never on real work in progress.
_ONESHOT_MAX_RUNTIME_S = 6 * 60 * 60


class UnknownServiceError(LookupError):
    """No container in the compose project carries this service label."""

    def __init__(self, service: str) -> None:
        super().__init__(service)
        self.service = service


class UpdateInProgressError(RuntimeError):
    """A one-shot (update, export, import, or reset) is already running.

    One-shots are mutually exclusive: an import mid-update, an export
    mid-import, or a reset mid-anything would race over the same database
    and files.
    """


@dataclass(frozen=True, slots=True)
class ContainerMemory:
    """Instantaneous memory usage of one compose container."""

    service: str
    mem_bytes: int


@dataclass(frozen=True, slots=True)
class ProcessMemory:
    """RSS of one process running inside a compose container.

    From `docker top`: the daemon runs ps on the HOST against the container's
    PIDs, so the RSS is the real host figure and it works even for an image with
    no ps binary. The breakdown a per-container total can't show — e.g. the
    local-llm container runs llama-swap plus a separate llama-server per loaded
    model, so 'what is the 120B vs the vision model' only resolves here."""

    service: str
    pid: int
    rss_bytes: int
    command: str


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """State of the most recent updater run ('none' when never run)."""

    state: str  # 'none' | 'running' | 'exited'
    exit_code: int | None
    log_tail: str


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """Status snapshot of one compose-managed container."""

    service: str
    state: str
    health: str | None
    started_at: str | None
    image: str


class DockerGateway(Protocol):
    """The full set of Docker operations the supervisor is allowed to perform."""

    def list_containers(self) -> list[ContainerInfo]: ...

    def restart(self, service: str) -> None: ...

    def start(self, service: str) -> None: ...

    def stop(self, service: str) -> None: ...

    def logs(self, service: str, tail: int) -> str: ...

    def stream_logs(self, service: str) -> Iterator[str]: ...

    def container_memory(self) -> list[ContainerMemory]: ...

    def container_processes(self) -> list[ProcessMemory]: ...

    def start_update(self) -> str: ...

    def update_status(self, tail: int) -> UpdateStatus: ...

    def start_export(self) -> str: ...

    def start_import(self, archive: str) -> str: ...

    def start_reset(self) -> str: ...

    def start_provision(self) -> str: ...

    def start_rebuild(self, service: str) -> str: ...

    def start_refresh(self, service: str) -> str: ...

    def start_perplexity(self, model_path: str, chunks: int | None) -> str: ...

    def oneshot_status(self, kind: str, tail: int) -> UpdateStatus: ...


class ComposeDockerGateway:
    """DockerGateway backed by the docker SDK, scoped to one compose project.

    Scoping is enforced by label filters on every lookup, so containers
    outside the project are invisible and uncontrollable by construction.
    """

    def __init__(
        self, client: docker.DockerClient, project: str, project_dir: str
    ) -> None:
        self._client = client
        self._project = project
        # Host path of the deploy dir; the updater mounts it at the SAME
        # path so compose's relative binds resolve to real host paths.
        self._project_dir = project_dir

    def list_containers(self) -> list[ContainerInfo]:
        containers = self._client.containers.list(
            all=True,
            filters={"label": f"{COMPOSE_PROJECT_LABEL}={self._project}"},
        )
        infos: list[ContainerInfo] = []
        for container in containers:
            labels = container.labels or {}
            service = labels.get(COMPOSE_SERVICE_LABEL)
            if not service or labels.get(COMPOSE_ONEOFF_LABEL) == "True":
                continue
            infos.append(_to_info(service, container))
        return infos

    def restart(self, service: str) -> None:
        self._find(service).restart()

    def start(self, service: str) -> None:
        # Acts on the EXISTING (created, stopped) container — the profile-gated
        # comfyui service is created by comfyui-setup.sh's `compose up`, so toggling
        # it on/off is a plain container start/stop. Unknown (never-created) 404s.
        self._find(service).start()

    def stop(self, service: str) -> None:
        self._find(service).stop()

    def logs(self, service: str, tail: int) -> str:
        raw: bytes = self._find(service).logs(tail=tail)
        return raw.decode("utf-8", errors="replace")

    def stream_logs(self, service: str) -> Iterator[str]:
        # tail=0: the stream carries only lines emitted after the client attaches.
        chunks = self._find(service).logs(stream=True, follow=True, tail=0)
        return _decode_lines(chunks)

    def container_memory(self) -> list[ContainerMemory]:
        usages: list[ContainerMemory] = []
        for container in self._client.containers.list(
            filters={"label": f"{COMPOSE_PROJECT_LABEL}={self._project}"}
        ):
            service = (container.labels or {}).get(COMPOSE_SERVICE_LABEL)
            if not service:
                continue
            try:
                # one_shot skips the 1s CPU sampling window; memory is instant.
                # docker-py types stats() as Iterator; stream=False returns a dict.
                stats = cast("dict", container.stats(stream=False, one_shot=True))
            except Exception:
                continue
            mem = stats.get("memory_stats", {})
            usage = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
            usages.append(ContainerMemory(service=service, mem_bytes=max(usage, 0)))
        return usages

    def container_processes(self) -> list[ProcessMemory]:
        procs: list[ProcessMemory] = []
        for container in self._client.containers.list(
            filters={"label": f"{COMPOSE_PROJECT_LABEL}={self._project}"}
        ):
            service = (container.labels or {}).get(COMPOSE_SERVICE_LABEL)
            if not service:
                continue
            try:
                # `args` carries the full command line, so two llama-server PIDs
                # are told apart by their --model path. The daemon runs ps on the
                # host; RSS is in KiB. A just-exited container (or a daemon that
                # rejects the ps_args) must not sink the whole readout.
                top = cast("dict", container.top(ps_args="-eo pid,rss,args"))
            except Exception:
                continue
            titles = top.get("Titles") or []
            try:
                pid_i, rss_i, cmd_i = (
                    titles.index("PID"),
                    titles.index("RSS"),
                    titles.index("COMMAND"),
                )
            except ValueError:
                continue
            for row in top.get("Processes") or []:
                if max(pid_i, rss_i, cmd_i) >= len(row):
                    continue
                try:
                    pid, rss_kib = int(row[pid_i]), int(row[rss_i])
                except ValueError:
                    continue
                procs.append(
                    ProcessMemory(
                        service=service,
                        pid=pid,
                        rss_bytes=rss_kib * 1024,
                        command=row[cmd_i],
                    )
                )
        return procs

    def start_update(self) -> str:
        return self._run_oneshot("jbrain-updater", {UPDATER_LABEL: "1"}, UPDATE_COMMAND)

    def update_status(self, tail: int) -> UpdateStatus:
        return self._status_of(self._latest(f"{UPDATER_LABEL}=1"), tail)

    def start_export(self) -> str:
        return self._run_oneshot(
            "jbrain-export", {ONESHOT_LABEL: "export"}, EXPORT_COMMAND
        )

    def start_import(self, archive: str) -> str:
        # The archive name is validated at the HTTP layer; quoting here keeps
        # this boundary safe even if a new caller forgets.
        command = f"exec sh src/deploy/import-inner.sh {shlex.quote(archive)}"
        return self._run_oneshot("jbrain-import", {ONESHOT_LABEL: "import"}, command)

    def start_reset(self) -> str:
        return self._run_oneshot(
            "jbrain-reset", {ONESHOT_LABEL: "reset"}, RESET_COMMAND
        )

    def start_provision(self) -> str:
        return self._run_oneshot(
            "jbrain-provision", {ONESHOT_LABEL: "provision"}, PROVISION_COMMAND
        )

    def start_rebuild(self, service: str) -> str:
        return self._run_oneshot(
            "jbrain-rebuild", {ONESHOT_LABEL: "rebuild"}, _rebuild_command(service)
        )

    def start_refresh(self, service: str) -> str:
        return self._run_oneshot(
            "jbrain-refresh", {ONESHOT_LABEL: "refresh"}, _refresh_command(service)
        )

    def start_perplexity(self, model_path: str, chunks: int | None) -> str:
        return self._run_oneshot(
            "jbrain-perplexity",
            {ONESHOT_LABEL: "perplexity"},
            _perplexity_command(self._project, model_path, chunks),
        )

    def oneshot_status(self, kind: str, tail: int) -> UpdateStatus:
        return self._status_of(self._latest(f"{ONESHOT_LABEL}={kind}"), tail)

    def _run_oneshot(self, prefix: str, labels: dict[str, str], command: str) -> str:
        if self._oneshot_running():
            raise UpdateInProgressError
        name = f"{prefix}-{int(time.time())}"
        self._client.containers.run(
            UPDATER_IMAGE,
            command=["sh", "-lc", command],
            name=name,
            detach=True,
            labels=labels,
            working_dir=self._project_dir,
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                self._project_dir: {"bind": self._project_dir, "mode": "rw"},
            },
        )
        return name

    def _oneshot_running(self) -> bool:
        for label in (f"{UPDATER_LABEL}=1", ONESHOT_LABEL):
            latest = self._latest(label)
            if latest is None:
                continue
            if not (latest.attrs or {}).get("State", {}).get("Running"):
                continue
            # A one-shot past the max runtime is hung, not in progress: reap it so it
            # can never wedge updates/provisions forever, then keep scanning (its slot
            # is now free). Only a genuinely in-flight one-shot blocks a new one.
            if self._oneshot_age_seconds(latest) > _ONESHOT_MAX_RUNTIME_S:
                self._reap(latest)
                continue
            return True
        return False

    def _oneshot_age_seconds(self, container: Container) -> float:
        """Seconds since a one-shot started, read from the epoch suffix baked into its
        name by _run_oneshot (`{prefix}-{int(time.time())}`) — robust and free of
        docker's RFC3339/nanosecond StartedAt parsing. An unparseable name reads as age
        0 (never reaped), so an unexpected name can only ever be over-cautious."""
        _, _, suffix = (container.name or "").rpartition("-")
        if not suffix.isdigit():
            return 0.0
        return max(0.0, time.time() - int(suffix))

    def _reap(self, container: Container) -> None:
        """Force-remove a hung one-shot so a fresh one can take its slot. Best-effort:
        a daemon hiccup or a container that just exited on its own must never raise
        into the start path — the worst case is the guard blocks one more time."""
        with contextlib.suppress(Exception):
            container.remove(force=True)

    def _status_of(self, container: Container | None, tail: int) -> UpdateStatus:
        if container is None:
            return UpdateStatus(state="none", exit_code=None, log_tail="")
        state = (container.attrs or {}).get("State", {})
        running = bool(state.get("Running"))
        raw: bytes = container.logs(tail=tail)
        return UpdateStatus(
            state="running" if running else "exited",
            exit_code=None if running else state.get("ExitCode"),
            log_tail=raw.decode("utf-8", errors="replace"),
        )

    def _latest(self, label: str) -> Container | None:
        matches = self._client.containers.list(all=True, filters={"label": label})
        if not matches:
            return None
        return max(matches, key=lambda c: (c.attrs or {}).get("Created", ""))

    def _find(self, service: str) -> Container:
        matches = self._client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{COMPOSE_PROJECT_LABEL}={self._project}",
                    f"{COMPOSE_SERVICE_LABEL}={service}",
                ]
            },
        )
        # A one-off alone is not the service: starting or stopping it would act on a
        # transient `compose run` container while the real one was never created.
        services = [
            c for c in matches if (c.labels or {}).get(COMPOSE_ONEOFF_LABEL) != "True"
        ]
        if not services:
            raise UnknownServiceError(service)
        return services[0]


def _to_info(service: str, container: Container) -> ContainerInfo:
    attrs = container.attrs or {}
    state = attrs.get("State", {})
    started_at = state.get("StartedAt")
    return ContainerInfo(
        service=service,
        state=state.get("Status", "unknown"),
        health=(state.get("Health") or {}).get("Status"),
        started_at=None if started_at == _NEVER_STARTED else started_at,
        image=attrs.get("Config", {}).get("Image", ""),
    )


def _decode_lines(chunks: Iterator[bytes]) -> Iterator[str]:
    # Docker yields arbitrary byte chunks, not lines; reassemble before decoding.
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")
    if buffer:
        yield buffer.decode("utf-8", errors="replace")
