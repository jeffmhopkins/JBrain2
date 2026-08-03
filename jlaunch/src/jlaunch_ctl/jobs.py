"""Job lifecycle: start a spec in a persistent PTY, track it, stop/kill it, finalize.

The analogue of jcode's ``SessionManager``, but for one-shot computations rather than
interactive sessions. The crucial differences:

* The PTY is created at **job start** and immediately fed the assembled script, so the
  job runs for hours with no browser attached; the WebSocket route only *attaches* to
  the already-running terminal (it never creates one).
* **No idle reaper.** A running job is never swept. A finished run's checkout (and its
  ~10 GB scratch) is kept until the owner deletes it — "keep the scratch until
  integration is confirmed."

Outcome is decided from the script's status file + the artifact/verify gates, never by
parsing terminal bytes (the scrollback ring is bounded and lost on EOF).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jlaunch_ctl.runner import build_script
from jlaunch_ctl.specs import SPECS, JobSpec, get_spec
from jlaunch_ctl.terminal import (
    PtyTerminal,
    TerminalRegistry,
    kill_process_group,
    kill_processes_in_dir,
)

_log = logging.getLogger("jlaunch_ctl.jobs")

# The tail of the durable console log kept on the job record for a post-mortem view once
# the terminal is gone (a reconnect after the job ends has no live PTY to attach to).
_CONSOLE_TAIL_BYTES = 16 * 1024

# States: queued (never used at max_concurrent_jobs=1 unless busy), cloning/running are
# live; succeeded/failed/killed are terminal.
LIVE_STATES = ("cloning", "running")


class JobError(RuntimeError):
    """A control operation failed — surfaced to the route as a clean message."""


class BusyError(JobError):
    """A job is already running and max_concurrent_jobs is reached."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def read_machine() -> dict[str, object]:
    """Best-effort machine specs for the run record (shown on the public results page):
    CPU model, core count, and total RAM. Unreadable fields are simply omitted."""
    machine: dict[str, object] = {"cores": os.cpu_count()}
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                machine["cpu"] = line.split(":", 1)[1].strip()
                break
    with contextlib.suppress(OSError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                machine["mem_bytes"] = kb * 1024
                break
    return machine


def signal_process_group(pid: int, sig: int) -> None:
    """Send ``sig`` to a PTY leader's whole process group (graceful SIGTERM stop). The
    group, not the bare pid, so the job's children get it too."""
    with contextlib.suppress(OSError):
        os.killpg(os.getpgid(pid), sig)
    with contextlib.suppress(OSError):
        os.kill(pid, sig)


@dataclass
class Job:
    id: str
    spec_name: str
    title: str
    repo: str
    state: str
    phase: str
    workspace: str
    artifact_name: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    verify_ok: bool | None = None
    headline: list[str] = field(default_factory=list)
    machine: dict[str, object] = field(default_factory=dict)
    phase_times: list[dict[str, object]] = field(default_factory=list)
    error: str | None = None
    artifact_present: bool = False
    artifact_bytes: int | None = None
    console_tail: str = ""

    def public(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("workspace", None)  # an internal path, not part of the control API
        return data


class JobManager:
    """Owns the live jobs: start, status, stop/kill, delete, artifact location."""

    def __init__(
        self,
        workspace_root: str,
        state_root: str,
        artifact_root: str,
        registry: TerminalRegistry,
        *,
        max_concurrent_jobs: int = 1,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._state_root = Path(state_root)
        self._artifact_root = Path(artifact_root)
        self._registry = registry
        self._max = max_concurrent_jobs
        self._jobs: dict[str, Job] = {}
        # Job ids whose stop/kill was owner-requested, so finalize records `killed`
        # rather than reading the (non-zero) rc as a spontaneous failure.
        self._killing: set[str] = set()
        # Live finalize tasks, held so the loop doesn't GC them mid-run.
        self._finalize_tasks: set[asyncio.Task[None]] = set()

    # -- queries -----------------------------------------------------------------

    def list(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, jid: str) -> Job:
        job = self._jobs.get(jid)
        if job is None:
            raise JobError(f"unknown job: {jid}")
        return job

    def _live_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.state in LIVE_STATES)

    # -- lifecycle ---------------------------------------------------------------

    async def start(self, spec_name: str) -> Job:
        spec = get_spec(spec_name)
        if spec is None:
            raise JobError(f"unknown spec: {spec_name}")
        if self._max > 0 and self._live_count() >= self._max:
            raise BusyError("a job is already running")

        jid = secrets.token_hex(4)
        workspace = self._workspace_root / jid
        state = self._state_root / jid
        workspace.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)

        job = Job(
            id=jid,
            spec_name=spec.name,
            title=spec.title,
            repo=spec.repo_url,
            state="cloning",
            phase="clone" if spec.repo_url else spec.phases[0].name,
            workspace=str(workspace),
            artifact_name=Path(spec.artifact_path).name,
            started_at=_now(),
            machine=read_machine(),
        )
        self._jobs[jid] = job

        (state / "job.sh").write_text(build_script(spec, str(state)))
        # Create the persistent PTY in the empty checkout, then feed it the job. `exec`
        # replaces the login shell with the job script IN PLACE, so the leader pid is
        # unchanged (kill-by-group still works) and the PTY EOFs — firing on_exit — the
        # moment the job finishes.
        term = PtyTerminal(str(workspace))
        self._registry.remove(jid)
        self._registry._terms[jid] = term  # we own this registry (same package)
        term.on_exit = self._on_exit(jid, spec)
        job.state = "running"
        await term.write(f"exec bash {shlex.quote(str(state / 'job.sh'))}\n".encode())
        _log.info("job start id=%s spec=%s pid=%d", jid, spec.name, term.pid)
        return job

    def _on_exit(self, jid: str, spec: JobSpec):
        def hook() -> None:
            # Runs in the reader callback (loop thread); schedule the async finalize and
            # hold the task so the loop doesn't GC it before it completes.
            task = asyncio.ensure_future(self._finalize(jid, spec))
            self._finalize_tasks.add(task)
            task.add_done_callback(self._finalize_tasks.discard)

        return hook

    async def _finalize(self, jid: str, spec: JobSpec) -> None:
        job = self._jobs.get(jid)
        if job is None or job.state not in LIVE_STATES:
            return
        state = self._state_root / jid
        workspace = Path(job.workspace)
        was_killed = jid in self._killing
        self._killing.discard(jid)
        job.finished_at = _now()

        status = _read_json(state / "status.json")
        rc = status.get("rc") if status else None
        if status and "phase" in status:
            job.phase = str(status["phase"])
        job.phase_times = _read_jsonl(state / "phase_times.jsonl")
        job.headline = _read_lines(state / "headline.txt")
        job.console_tail = _read_tail(state / "console.log")

        artifact = workspace / spec.artifact_path
        if artifact.is_file():
            job.artifact_present = True
            job.artifact_bytes = artifact.stat().st_size
            # Copy the deliverable out of the checkout so it survives independent of it.
            with contextlib.suppress(OSError):
                self._artifact_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact, self._artifact_dest(jid, spec))
        job.verify_ok = _verify_ok(
            workspace / spec.verify_log, spec.verify_must_end_with
        )

        if was_killed:
            job.state = "killed"
        elif rc == 0 and job.artifact_present and job.verify_ok:
            job.state = "succeeded"
        else:
            job.state = "failed"
            job.error = job.error or _failure_reason(rc, job)
        _log.info("job finish id=%s state=%s rc=%s", jid, job.state, rc)

    async def stop(self, jid: str) -> Job:
        """Graceful stop: SIGTERM the job's process group; the PTY EOF finalizes it as
        `killed`. A no-op on an already-finished job."""
        job = self.get(jid)
        if job.state in LIVE_STATES:
            self._killing.add(jid)
            term = self._registry.get(jid)
            if term is not None:
                signal_process_group(term.pid, signal.SIGTERM)
        return job

    async def kill(self, jid: str) -> Job:
        """Hard kill: SIGKILL the group and sweep any process still in the checkout;
        the PTY EOF then finalizes it as `killed`."""
        job = self.get(jid)
        if job.state in LIVE_STATES:
            self._killing.add(jid)
            term = self._registry.get(jid)
            if term is not None:
                kill_process_group(term.pid)
            kill_processes_in_dir(job.workspace)
        return job

    async def delete(self, jid: str) -> None:
        """Kill if live, then remove the checkout, state, and artifact. Idempotent."""
        job = self._jobs.get(jid)
        if job is None:
            return
        if job.state in LIVE_STATES:
            with contextlib.suppress(JobError):
                await self.kill(jid)
        term = self._registry.get(jid)
        if term is not None:
            term.close()
        self._registry.remove(jid)
        shutil.rmtree(job.workspace, ignore_errors=True)
        shutil.rmtree(self._state_root / jid, ignore_errors=True)
        spec = get_spec(job.spec_name) or SPECS.get(job.spec_name)
        if spec is not None:
            with contextlib.suppress(OSError):
                self._artifact_dest(jid, spec).unlink(missing_ok=True)
        self._jobs.pop(jid, None)
        self._killing.discard(jid)

    # -- artifact ----------------------------------------------------------------

    def _artifact_dest(self, jid: str, spec: JobSpec) -> Path:
        return self._artifact_root / f"{jid}-{Path(spec.artifact_path).name}"

    def artifact_path(self, jid: str) -> Path:
        """Path to a finished job's collected artifact, or raise if it isn't there."""
        job = self.get(jid)
        spec = get_spec(job.spec_name)
        if spec is None or not job.artifact_present:
            raise JobError("no artifact for this job")
        dest = self._artifact_dest(jid, spec)
        if dest.is_file():
            return dest
        # Fall back to the in-checkout copy if the out-copy didn't happen.
        src = Path(job.workspace) / spec.artifact_path
        if src.is_file():
            return src
        raise JobError("artifact file is missing")

    def artifact_name(self, jid: str) -> str:
        spec = get_spec(self.get(jid).spec_name)
        return Path(spec.artifact_path).name if spec else "artifact"

    def close_all(self) -> None:
        self._registry.close_all()


def _read_json(path: Path) -> dict[str, object] | None:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return json.loads(path.read_text())
    return None


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
    return out


def _read_lines(path: Path) -> list[str]:
    with contextlib.suppress(OSError):
        return [ln for ln in path.read_text().splitlines() if ln.strip()]
    return []


def _read_tail(path: Path, limit: int = _CONSOLE_TAIL_BYTES) -> str:
    with contextlib.suppress(OSError):
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", errors="replace")
    return ""


def _verify_ok(path: Path, sentinel: str) -> bool | None:
    """True/False if the verify log exists (its tail carries the sentinel or not); None
    if the spec has no verify log or it was never written (e.g. killed early)."""
    if not path.is_file():
        return None
    tail = _read_tail(path)
    return sentinel in tail


def _failure_reason(rc: object, job: Job) -> str:
    if not job.artifact_present:
        return f"no artifact produced (rc={rc})"
    if job.verify_ok is False:
        return f"verification did not pass (rc={rc})"
    return f"job exited with rc={rc}"
