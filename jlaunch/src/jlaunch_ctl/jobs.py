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
import re
import secrets
import shlex
import shutil
import signal
import stat
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jlaunch_ctl.runner import build_script
from jlaunch_ctl.specs import JobSpec, get_spec

if TYPE_CHECKING:
    from jlaunch_ctl.catalog import Catalog
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
# What a live folder may serve. The job is untrusted code that can put anything in its
# checkout, so a name must be plain (no paths, no dotfiles such as the trainer's
# write-then-rename temporaries) and of a known type, and only regular files are read.
_LIVE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LIVE_TYPES = {
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".png": "image/png",
    ".gz": "application/gzip",
}
LIVE_MAX_BYTES = 32 * 1024 * 1024

# States: cloning/running are live; succeeded/failed/killed are terminal.
LIVE_STATES = ("cloning", "running")
# How long a Start waits on re-reading the training repo before using the list it has.
START_REFRESH_S = 15.0
# A job's id (secrets.token_hex(4)): the only state-root entries read back as runs.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8}$")
# Each run's record, in its state folder, so a restart (every Ops -> Update) doesn't
# forget the finished runs: their results and 3D replays were on disk all along, but the
# record lived only in memory, so the app showed the run with nothing to open (owner,
# 2026-11-14).
_RECORD = "job.json"


class JobError(RuntimeError):
    """A control operation failed — surfaced to the route as a clean message."""


class BusyError(JobError):
    """No room for this run: one that needs the whole box is live, this one needs it, or
    max_concurrent_jobs runs are already going."""


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
    # The training repo commit a manifest job cloned; empty for a built-in spec.
    commit: str = ""
    # The spec's live folder and artifact, inside the checkout, kept on the record so a
    # finished run still serves them after its spec has changed or left the list.
    live_dir: str = ""
    artifact_path: str = ""
    # Copied from the spec at start, so a live run's claim on the box holds after its
    # spec leaves the list.
    shares_box: bool = False
    # The core share a shared run is pinned to (its index among max_concurrent_jobs);
    # -1 for an unpinned run.
    cpu_slot: int = -1

    def public(self) -> dict[str, object]:
        data = asdict(self)
        # Internal, not part of the control API.
        for key in ("workspace", "live_dir", "artifact_path", "shares_box", "cpu_slot"):
            data.pop(key, None)
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
        max_concurrent_jobs: int = 3,
        catalog: Catalog | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._state_root = Path(state_root)
        self._artifact_root = Path(artifact_root)
        self._registry = registry
        # At least one: the slots and the core shares are counted from it.
        self._max = max(1, max_concurrent_jobs)
        self._catalog = catalog
        self._jobs: dict[str, Job] = {}
        # Job ids whose stop/kill was owner-requested, so finalize records `killed`
        # rather than reading the (non-zero) rc as a spontaneous failure.
        self._killing: set[str] = set()
        # Live finalize tasks, held so the loop doesn't GC them mid-run.
        self._finalize_tasks: set[asyncio.Task[None]] = set()
        self._load_records()

    # -- records -----------------------------------------------------------------

    def _save(self, job: Job) -> None:
        # The job can write its state folder, and this runs on the event loop: never
        # follow a symlink there (the folder itself, or one planted at the temp name)
        # and never open anything that could block, such as a FIFO.
        tmp = f".{_RECORD}.tmp"
        try:
            data = json.dumps(asdict(job)).encode()
            folder = os.open(
                self._state_root / job.id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except (OSError, ValueError, RecursionError):
            _log.warning("job record not saved id=%s", job.id, exc_info=True)
            return
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=folder)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(tmp, flags, 0o600, dir_fd=folder)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            finally:
                os.close(fd)
            os.replace(tmp, _RECORD, src_dir_fd=folder, dst_dir_fd=folder)
        except OSError:
            _log.warning("job record not saved id=%s", job.id, exc_info=True)
        finally:
            os.close(folder)

    def _load_records(self) -> None:
        """Read back the runs a previous control server started.

        The job can write to its own state folder, so a record is data, never trusted
        for a path: the id comes from the folder's name and the checkout is rebuilt
        from it, and the live folder and artifact are confined to that checkout and the
        artifact root where they are used. A run that was live when the server went
        down has no terminal now, so the first poll reaps it as lost (or finished, if
        it wrote its exit code meanwhile)."""
        try:
            entries = sorted(os.scandir(self._state_root), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if not _JOB_ID_RE.match(entry.name) or not entry.is_dir(
                follow_symlinks=False
            ):
                continue
            # One bad record (deep nesting, a huge number, junk types) must never stop
            # the server starting: it would fail the same way after every Update.
            try:
                data = _read_record(Path(entry.path) / _RECORD)
                job = _job_from_record(
                    entry.name, data, self._workspace_root / entry.name
                )
            except Exception:
                _log.warning("job record skipped id=%s", entry.name, exc_info=True)
                continue
            if job is not None:
                self._jobs[job.id] = job

    # -- queries -----------------------------------------------------------------

    def list(self) -> list[Job]:
        for job in self._jobs.values():
            self._reap(job)
            self._refresh_phase(job)
        return list(self._jobs.values())

    def get(self, jid: str) -> Job:
        job = self._jobs.get(jid)
        if job is None:
            raise JobError(f"unknown job: {jid}")
        self._reap(job)
        self._refresh_phase(job)
        return job

    def _refresh_phase(self, job: Job) -> None:
        # Without this a running job showed "clone" its whole run, which read as stuck.
        if job.state not in LIVE_STATES:
            return
        path = self._state_root / job.id / "phase"
        # The job can write here, and this runs on every poll: only a regular file, and
        # only a short read, so a FIFO or a huge file can't stall the event loop.
        with contextlib.suppress(OSError):
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as fh:
                    phase = fh.read(64).decode("utf-8", errors="replace").strip()
                if phase:
                    job.phase = phase

    def _listed(self, name: str) -> bool:
        return self._catalog is not None and self._catalog.owns(name)

    def _check_room(self, spec: JobSpec) -> None:
        # Reaped first: a stuck orphan must not hold a slot, refusing Start.
        for job in self._jobs.values():
            self._reap(job)
        live = [j for j in self._jobs.values() if j.state in LIVE_STATES]
        if not live:
            return
        if not spec.shares_box or not all(j.shares_box for j in live):
            raise BusyError(
                "a job that needs the whole machine is running or asked for"
            )
        # Two live runs of one spec would race for the same run number in the api's
        # mirror, and never help: a variant to compare is its own job in jobs.toml.
        if any(j.spec_name == spec.name for j in live):
            raise BusyError(f"{spec.name} is already running")
        if len(live) >= self._max:
            raise BusyError(f"{len(live)} jobs are already running")

    def _free_slot(self) -> int:
        taken = {
            j.cpu_slot
            for j in self._jobs.values()
            if j.state in LIVE_STATES and j.cpu_slot >= 0
        }
        return next(i for i in range(self._max) if i not in taken)

    # -- lifecycle ---------------------------------------------------------------

    async def start(self, spec_name: str) -> Job:
        known = get_spec(spec_name)
        if known is not None:
            self._check_room(known)
        if self._catalog is not None and self._catalog.owns(spec_name):
            # A config fix pushed a minute ago is what should train; an unreachable repo
            # falls back to the commit the list already shows.
            # Bounded: the api gives up on a Start after 30 s, and a run that started
            # after the caller gave up wouldn't reach its run list until the next poll.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._catalog.refresh_quietly(), START_REFRESH_S)
            if not self._catalog.owns(spec_name):
                raise JobError(f"{spec_name} is no longer in the training repo's jobs")
        spec = get_spec(spec_name)
        if spec is None or (spec.commit and not self._listed(spec_name)):
            raise JobError(f"unknown spec: {spec_name}")
        # The refresh above awaited: a second Start may have got in meanwhile.
        self._check_room(spec)

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
            commit=spec.commit,
            live_dir=spec.live_dir or "",
            artifact_path=spec.artifact_path,
            shares_box=spec.shares_box,
            cpu_slot=self._free_slot() if spec.shares_box else -1,
        )
        self._jobs[jid] = job
        self._save(job)

        (state / "job.sh").write_text(build_script(spec, str(state)))
        # Create the persistent PTY in the empty checkout, then feed it the job. `exec`
        # replaces the login shell with the job script IN PLACE, so the leader pid is
        # unchanged (kill-by-group still works) and the PTY EOFs — firing on_exit — the
        # moment the job finishes.
        term = PtyTerminal(str(workspace))
        if job.cpu_slot >= 0 and not self._pin(job, term.pid):
            # Unpinned, it would take every core from the runs that are pinned: never
            # start it, and say why on the run.
            term.close()
            job.state = "failed"
            job.error = "couldn't pin the run to its own cores"
            job.finished_at = _now()
            self._save(job)
            return job
        self._registry.remove(jid)
        self._registry._terms[jid] = term  # we own this registry (same package)
        term.on_exit = self._on_exit(jid, spec)
        job.state = "running"
        await term.write(f"exec bash {shlex.quote(str(state / 'job.sh'))}\n".encode())
        _log.info("job start id=%s spec=%s pid=%d", jid, spec.name, term.pid)
        return job

    def _pin(self, job: Job, pid: int) -> bool:
        """Pin a shared run to its own cores, before it starts: every process the job
        forks inherits them, so side-by-side runs never contend for a core, and a run
        that sizes itself to the cores it may use sizes itself to its share."""
        cpus = cpu_share(job.cpu_slot, self._max)
        try:
            os.sched_setaffinity(pid, cpus)
        except OSError:
            _log.warning("job not pinned id=%s", job.id, exc_info=True)
            return False
        job.machine["pinned_cpus"] = cpu_list(cpus)
        job.machine["pinned_cores"] = len(cpus)
        self._save(job)
        return True

    def _on_exit(self, jid: str, spec: JobSpec):
        def hook() -> None:
            # Runs in the reader callback (loop thread); schedule the async finalize and
            # hold the task so the loop doesn't GC it before it completes.
            task = asyncio.ensure_future(self._finalize(jid, spec))
            self._finalize_tasks.add(task)
            task.add_done_callback(self._finalize_tasks.discard)

        return hook

    async def _finalize(self, jid: str, spec: JobSpec) -> None:
        self._finish(jid, spec)

    def _finish(self, jid: str, spec: JobSpec | None, *, reaped: bool = False) -> None:
        """Record a job's end. Synchronous, so it never interleaves with a poll, and it
        always leaves the job out of the live states: a run whose finalize raised stayed
        "running" with nothing to kill, undeletable (owner, 2026-11-14)."""
        job = self._jobs.get(jid)
        if job is None or job.state not in LIVE_STATES:
            return
        try:
            self._end(job, spec, reaped=reaped)
        finally:
            self._save(job)

    def _end(self, job: Job, spec: JobSpec | None, *, reaped: bool) -> None:
        jid = job.id
        was_killed = jid in self._killing
        self._killing.discard(jid)
        job.finished_at = _now()
        try:
            if spec is None:
                raise JobError(f"spec {job.spec_name!r} is no longer registered")
            rc = self._collect(job, spec)
        except Exception as exc:  # any failure here must still end the job
            _log.exception("job finalize failed id=%s", jid)
            job.state = "killed" if was_killed else "failed"
            job.error = job.error or f"finalize failed: {exc}"
            return
        if was_killed:
            job.state = "killed"
        elif rc == 0 and job.artifact_present and job.verify_ok:
            job.state = "succeeded"
        else:
            job.state = "failed"
            # A reaped run that never wrote its exit code ended without its script
            # finishing: say so, rather than the bare "no artifact (rc=None)".
            lost = reaped and rc is None
            job.error = job.error or (
                "the run's terminal was lost before it finished"
                if lost
                else _failure_reason(rc, job)
            )
        _log.info("job finish id=%s state=%s rc=%s", jid, job.state, rc)

    def _collect(self, job: Job, spec: JobSpec) -> object:
        """Read what the finished job left (phase, timings, headline, console, artifact)
        onto it, and return its exit code (None when it never wrote one)."""
        state = self._state_root / job.id
        workspace = Path(job.workspace)
        # A killed job never writes status.json; this keeps the phase it was killed in.
        self._refresh_phase(job)
        status = _read_json(state / "status.json")
        rc = status.get("rc") if status else None
        if status and "phase" in status:
            job.phase = str(status["phase"])
        job.phase_times = _flat_rows(_read_jsonl(state / "phase_times.jsonl"))
        job.headline = _short_lines(_read_lines(state / "headline.txt"))
        job.console_tail = _read_tail(state / "console.log")

        artifact = workspace / (self._artifact_rel(job) or spec.artifact_path)
        if artifact.is_file():
            job.artifact_present = True
            job.artifact_bytes = artifact.stat().st_size
            # Copy the deliverable out of the checkout so it survives independent of it.
            with contextlib.suppress(OSError):
                self._artifact_root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(artifact, self._artifact_dest(job))
        job.verify_ok = _verify_ok(
            workspace / spec.verify_log, spec.verify_must_end_with
        )
        return rc

    def _reap(self, job: Job) -> None:
        """A live job whose terminal is gone has nothing left running it: its PTY exited
        but the end was never recorded. Record it now, so it can't sit "running" with a
        Kill that finds nothing to kill."""
        if job.state in LIVE_STATES and self._registry.get(job.id) is None:
            self._finish(job.id, get_spec(job.spec_name), reaped=True)

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
        with contextlib.suppress(OSError):
            self._artifact_dest(job).unlink(missing_ok=True)
        self._jobs.pop(jid, None)
        self._killing.discard(jid)

    # -- artifact ----------------------------------------------------------------

    def _artifact_rel(self, job: Job) -> str:
        # The run's own, as its spec was at start: the spec may have moved it since.
        if job.artifact_path:
            return job.artifact_path
        spec = get_spec(job.spec_name)
        return spec.artifact_path if spec is not None else ""

    def _artifact_dest(self, job: Job) -> Path:
        name = Path(self._artifact_rel(job)).name or "artifact"
        return self._artifact_root / f"{job.id}-{name}"

    def artifact_path(self, jid: str) -> Path:
        """Path to a finished job's collected artifact, or raise if it isn't there."""
        job = self.get(jid)
        rel = self._artifact_rel(job)
        if not rel or not job.artifact_present:
            raise JobError("no artifact for this job")
        dest = self._artifact_dest(job)
        if dest.is_file():
            return dest
        # Fall back to the in-checkout copy if the out-copy didn't happen.
        workspace = Path(job.workspace).resolve()
        src = (workspace / rel).resolve()
        if src.is_relative_to(workspace) and src.is_file():
            return src
        raise JobError("artifact file is missing")

    def artifact_name(self, jid: str) -> str:
        return Path(self._artifact_rel(self.get(jid))).name or "artifact"

    # -- live folder -------------------------------------------------------------

    def _live_dir(self, jid: str) -> Path:
        job = self.get(jid)
        live_dir = job.live_dir
        if not live_dir:
            spec = get_spec(job.spec_name)
            live_dir = spec.live_dir if spec is not None else ""
        if not live_dir:
            raise JobError("this job has no live folder")
        workspace = Path(job.workspace).resolve()
        folder = (workspace / live_dir).resolve()
        # The job could make the folder (or a parent) a symlink out of its checkout.
        if not folder.is_relative_to(workspace):
            raise JobError("live folder is outside the checkout")
        return folder

    def live_files(self, jid: str) -> list[dict[str, object]]:
        """The servable files in a job's live folder, newest first. Empty until the job
        has written any."""
        folder = self._live_dir(jid)
        files: list[dict[str, object]] = []
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return files
        for entry in entries:
            if not _live_name_ok(entry.name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files.append(
                {
                    "name": entry.name,
                    "bytes": st.st_size,
                    "modified_at": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                }
            )
        files.sort(key=lambda f: str(f["modified_at"]), reverse=True)
        return files

    def read_live(self, jid: str, name: str) -> tuple[bytes, str]:
        """One live file's bytes and media type. Opened without following a symlink and
        read whole, so what is checked is what is served."""
        if not _live_name_ok(name):
            raise JobError("no such live file")
        folder = self._live_dir(jid)
        try:
            fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            raise JobError("no such live file") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise JobError("no such live file")
            if st.st_size > LIVE_MAX_BYTES:
                raise JobError("live file is too large")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read(LIVE_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > LIVE_MAX_BYTES:
            raise JobError("live file is too large")
        return data, LIVE_TYPES[Path(name).suffix]

    def close_all(self) -> None:
        self._registry.close_all()


_RECORD_MAX_BYTES = 1024 * 1024
_RECORD_STATES = frozenset({"cloning", "running", "succeeded", "failed", "killed"})


def _read_record(path: Path) -> dict[str, object] | None:
    """A job record, or None. Opened without following a symlink or blocking on a FIFO,
    and size-capped, as the job can plant anything there; any parse error is the
    caller's to skip."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _RECORD_MAX_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(_RECORD_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _RECORD_MAX_BYTES:
        return None
    data = json.loads(raw.decode("utf-8", errors="replace"))
    return data if isinstance(data, dict) else None


# What the job's own files may put on its record: flat rows of plain values, a bounded
# number of short lines. Nested values could be deep enough to fail serialising the run
# list (every run then vanishes from the app), and a record past _RECORD_MAX_BYTES
# would be skipped on reload.
_MAX_ROWS = 256
_MAX_LINE = 512


def _flat(row: dict) -> dict[str, object]:
    scalar = (str, int, float, bool, type(None))
    out: dict[str, object] = {}
    for k, v in list(row.items())[:64]:
        if isinstance(k, str) and isinstance(v, scalar):
            out[k[:_MAX_LINE]] = v[:_MAX_LINE] if isinstance(v, str) else v
    return out


def _flat_rows(rows: list) -> list[dict[str, object]]:
    return [_flat(r) for r in rows[:_MAX_ROWS] if isinstance(r, dict)]


def _short_lines(lines: list[str]) -> list[str]:
    return [line[:_MAX_LINE] for line in lines[:_MAX_ROWS]]


def _rel_path(value: object) -> str:
    # A checkout-relative path, or nothing: never absolute, never up and out, no NUL.
    if not isinstance(value, str) or not value or "\0" in value:
        return ""
    parts = Path(value).parts
    if Path(value).is_absolute() or ".." in parts:
        return ""
    return value


def _job_from_record(
    jid: str, data: dict[str, object] | None, workspace: Path
) -> Job | None:
    if not data:
        return None

    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    def opt_text(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) else None

    def rows(key: str) -> list:
        value = data.get(key)
        return _flat_rows(value) if isinstance(value, list) else []

    state = text("state")
    if not state or not text("spec_name"):
        return None
    # The api treats states it doesn't know (such as "queued") as live and refuses to
    # delete them, and jlaunch never reaps them: an unknown state ends as failed.
    if state not in _RECORD_STATES:
        state = "failed"
    machine = data.get("machine")
    verify = data.get("verify_ok")
    size = data.get("artifact_bytes")
    headline = data.get("headline")
    return Job(
        id=jid,
        spec_name=text("spec_name"),
        title=text("title"),
        repo=text("repo"),
        state=state,
        phase=text("phase"),
        workspace=str(workspace),
        artifact_name=text("artifact_name"),
        started_at=opt_text("started_at"),
        finished_at=opt_text("finished_at"),
        verify_ok=verify if isinstance(verify, bool) else None,
        headline=_short_lines([h for h in headline if isinstance(h, str)])
        if isinstance(headline, list)
        else [],
        machine=_flat(machine) if isinstance(machine, dict) else {},
        phase_times=rows("phase_times"),
        error=opt_text("error"),
        artifact_present=data.get("artifact_present") is True,
        shares_box=data.get("shares_box") is True,
        cpu_slot=_slot(data.get("cpu_slot")),
        artifact_bytes=size
        if isinstance(size, int) and not isinstance(size, bool)
        else None,
        console_tail=text("console_tail"),
        commit=text("commit"),
        live_dir=_rel_path(data.get("live_dir")),
        artifact_path=_rel_path(data.get("artifact_path")),
    )


def _live_name_ok(name: str) -> bool:
    return bool(_LIVE_NAME_RE.match(name)) and Path(name).suffix in LIVE_TYPES


def _read_text(path: Path) -> str:
    # The job writes these files: a stray non-UTF-8 byte must not raise out of finalize
    # (which then never recorded the end, leaving the run "running" for good), and only
    # a regular file is read, so a FIFO planted under the name can't stall a reap.
    if not path.is_file() or path.is_symlink():
        raise OSError(f"not a regular file: {path.name}")
    return path.read_bytes().decode("utf-8", errors="replace")


def _read_json(path: Path) -> dict[str, object] | None:
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(_read_text(path))
        return data if isinstance(data, dict) else None
    return None


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    with contextlib.suppress(OSError):
        for line in _read_text(path).splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                row = json.loads(line)
                if isinstance(row, dict):
                    out.append(row)
    return out


def _read_lines(path: Path) -> list[str]:
    with contextlib.suppress(OSError):
        return [ln for ln in _read_text(path).splitlines() if ln.strip()]
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


def _slot(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 64:
        return value
    return -1


def _sibling_groups(cpus: set[int]) -> list[list[int]]:
    """The usable CPUs grouped by physical core (a core's SMT threads together), in
    order, so a share never splits a core with another run."""
    groups: dict[str, list[int]] = {}
    for cpu in sorted(cpus):
        path = f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
        try:
            with open(path) as fh:
                key = fh.read().strip()
        except OSError:
            # Unknown topology: one CPU per group, so shares stay disjoint by CPU even
            # if they might then split a core's two threads.
            _log.warning("no CPU topology for cpu%d; splitting by thread", cpu)
            key = str(cpu)
        groups.setdefault(key, []).append(cpu)
    return sorted(groups.values(), key=lambda g: g[0])


def cpu_share(slot: int, slots: int) -> set[int]:
    """Slot ``slot`` of ``slots`` equal, disjoint shares of this process's CPUs, split
    on whole cores. A machine with fewer cores than slots gives every slot one core."""
    groups = _sibling_groups(os.sched_getaffinity(0))
    n = len(groups)
    if n < slots:
        return set(groups[slot % n])
    lo, hi = slot * n // slots, (slot + 1) * n // slots
    return {cpu for group in groups[lo:hi] for cpu in group}


def cpu_list(cpus: set[int]) -> str:
    """``{0,1,2,16,17}`` as ``0-2,16-17``, the form taskset and cpuset read."""
    out: list[str] = []
    for cpu in sorted(cpus):
        if out and int(out[-1].split("-")[-1]) == cpu - 1:
            out[-1] = f"{out[-1].split('-')[0]}-{cpu}"
        else:
            out.append(str(cpu))
    return ",".join(out)
