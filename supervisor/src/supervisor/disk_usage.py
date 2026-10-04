"""Where did the disk go? A breakdown the owner can read without a terminal.

Three views, each best-effort so one failing never hides the others:

- `filesystem`: statvfs totals. The supervisor's own `/` is an overlay whose statfs
  is the docker data root's filesystem, so it is folded into that entry when their
  totals match; PROJECT_DIR and the data root are stat'ed from a helper container that
  bind-mounts them, and folded by filesystem id when they share a disk.
- `docker`: the daemon's own `docker system df` payload — images, writable layers,
  volumes, build cache.
- `project_dirs`: `du` over PROJECT_DIR's top level, one level deeper under the
  model and backup dirs, run in a read-only, network-less helper container (the
  supervisor itself does not mount the project tree).

The result is cached in-process so a polling client cannot keep a `du` running.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from supervisor.gateway import HelperRun

CACHE_TTL_S = 60.0
# Sized so df (the docker client's 60 s default), stat and du together fit the
# backend's 240 s read budget. A cold build may still outlast the tunnel's ~100 s edge
# limit; the build finishes here regardless and the retry reads it from the cache.
# A du that hits this cap is killed and reported PARTIAL: du prints each directory as
# it finishes it, so the entries done by then are listed and the rest (and the total)
# are missing. Retrying cannot complete it — the same cap applies every time.
DU_TIMEOUT_S = 140.0
STAT_TIMEOUT_S = 20.0
PROJECT_MOUNT = "/mnt/project"
DOCKER_ROOT_MOUNT = "/mnt/docker-root"
# Dirs whose children are the interesting unit (one model, one backup), so the
# breakdown goes one level deeper under them.
DEEP_DIRS = frozenset({"local-models", "comfyui-models", "whisper-models", "backups"})
TOP_IMAGES = 15
TOP_CONTAINERS = 10
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_VOLUME_LABEL = "com.docker.compose.volume"
_ERR_TAIL = 400


class DiskProbe(Protocol):
    """The docker reads the breakdown needs; ComposeDockerGateway implements it."""

    def docker_df(self) -> dict[str, Any]: ...

    def docker_root_dir(self) -> str | None: ...

    def run_readonly_helper(
        self, argv: Sequence[str], mounts: Mapping[str, str], timeout_s: float
    ) -> HelperRun: ...


class FilesystemOut(BaseModel):
    # Every path known to live on this filesystem; more than one means it was folded.
    paths: list[str]
    fsid: str | None
    total_bytes: int
    used_bytes: int
    free_bytes: int


class ImageOut(BaseModel):
    id: str
    repo_tags: list[str]
    created: str | None
    size_bytes: int
    # Layers this image shares with others; None when the daemon did not compute it.
    shared_bytes: int | None
    # What removing this image alone would free (size minus shared layers).
    unique_bytes: int | None
    containers: int | None
    in_use: bool


class ImagesOut(BaseModel):
    count: int
    in_use_count: int
    # LayersSize from the daemon: every layer counted once. Summing per-image sizes
    # instead counts each shared layer once per image that uses it.
    total_bytes: int
    total_source: str  # "LayersSize" | "sum_of_sizes"
    # With LayersSize: docker system df's own figure, LayersSize minus the unique bytes
    # of images a container uses. An UPPER BOUND — layers an in-use image shares with
    # others are counted as reclaimable though pruning cannot free them. Without it:
    # the unique bytes of unused images, a lower bound.
    reclaimable_bytes: int
    top: list[ImageOut]


class ContainerOut(BaseModel):
    name: str
    image: str
    state: str
    size_rw_bytes: int
    size_rootfs_bytes: int | None
    project: bool


class ContainersOut(BaseModel):
    count: int
    total_rw_bytes: int
    # Writable layers of containers that are not running.
    reclaimable_bytes: int
    top: list[ContainerOut]


class VolumeOut(BaseModel):
    name: str
    size_bytes: int | None
    ref_count: int | None
    project: bool
    compose_volume: str | None


class VolumesOut(BaseModel):
    count: int
    total_bytes: int
    project_bytes: int
    # Volumes no container references.
    reclaimable_bytes: int
    items: list[VolumeOut]


class BuildCacheOut(BaseModel):
    count: int
    total_bytes: int
    reclaimable_bytes: int


class DockerOut(BaseModel):
    root_dir: str | None
    images: ImagesOut
    containers: ContainersOut
    volumes: VolumesOut
    build_cache: BuildCacheOut


class DirSizeOut(BaseModel):
    path: str
    bytes: int


class ProjectDirsOut(BaseModel):
    root: str
    total_bytes: int | None
    entries: list[DirSizeOut]


class DiskReport(BaseModel):
    generated_at: str
    cached: bool = False
    # True when another build was in flight and this older one was served instead of
    # waiting for it; ask again shortly for the fresh one.
    stale: bool = False
    age_s: float = 0.0
    filesystem: list[FilesystemOut]
    docker: DockerOut | None
    project_dirs: ProjectDirsOut | None
    errors: list[str]


def _int(value: object) -> int | None:
    """A daemon size field, or None for absent / the -1 'not computed' sentinel."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _created(value: object) -> str | None:
    if isinstance(value, int) and value > 0:
        return datetime.fromtimestamp(value, UTC).isoformat()
    return None


def parse_images(payload: dict[str, Any]) -> ImagesOut:
    images: list[ImageOut] = []
    for raw in payload.get("Images") or []:
        size = _int(raw.get("Size")) or 0
        shared = _int(raw.get("SharedSize"))
        containers = _int(raw.get("Containers"))
        tags = [t for t in (raw.get("RepoTags") or []) if t and t != "<none>:<none>"]
        images.append(
            ImageOut(
                id=str(raw.get("Id", "")).removeprefix("sha256:")[:12],
                repo_tags=tags,
                created=_created(raw.get("Created")),
                size_bytes=size,
                shared_bytes=shared,
                unique_bytes=None if shared is None else max(size - shared, 0),
                containers=containers,
                # Uncomputed (-1) reads as in use: never call an image free to prune
                # on a count the daemon did not take.
                in_use=containers is None or containers > 0,
            )
        )
    layers = _int(payload.get("LayersSize"))
    unused = [i for i in images if not i.in_use]
    if layers is not None:
        # docker's formula, except an unknown SharedSize subtracts the whole image
        # (docker skips it, which would inflate the figure further).
        used = sum(i.size_bytes - (i.shared_bytes or 0) for i in images if i.in_use)
        reclaimable = max(layers - used, 0)
    else:
        reclaimable = sum(
            i.unique_bytes if i.unique_bytes is not None else i.size_bytes
            for i in unused
        )
    images.sort(key=lambda i: i.size_bytes, reverse=True)
    return ImagesOut(
        count=len(images),
        in_use_count=len(images) - len(unused),
        total_bytes=layers if layers is not None else sum(i.size_bytes for i in images),
        total_source="LayersSize" if layers is not None else "sum_of_sizes",
        reclaimable_bytes=reclaimable,
        top=images[:TOP_IMAGES],
    )


def parse_containers(payload: dict[str, Any], project: str) -> ContainersOut:
    items: list[ContainerOut] = []
    for raw in payload.get("Containers") or []:
        names = raw.get("Names") or []
        labels = raw.get("Labels") or {}
        name = str(names[0]).lstrip("/") if names else str(raw.get("Id", ""))[:12]
        items.append(
            ContainerOut(
                name=name,
                image=str(raw.get("Image", "")),
                state=str(raw.get("State", "")),
                size_rw_bytes=_int(raw.get("SizeRw")) or 0,
                size_rootfs_bytes=_int(raw.get("SizeRootFs")),
                project=labels.get(COMPOSE_PROJECT_LABEL) == project,
            )
        )
    items.sort(key=lambda c: c.size_rw_bytes, reverse=True)
    return ContainersOut(
        count=len(items),
        total_rw_bytes=sum(c.size_rw_bytes for c in items),
        reclaimable_bytes=sum(c.size_rw_bytes for c in items if c.state != "running"),
        top=items[:TOP_CONTAINERS],
    )


def parse_volumes(payload: dict[str, Any], project: str) -> VolumesOut:
    items: list[VolumeOut] = []
    for raw in payload.get("Volumes") or []:
        labels = raw.get("Labels") or {}
        usage = raw.get("UsageData") or {}
        items.append(
            VolumeOut(
                name=str(raw.get("Name", "")),
                size_bytes=_int(usage.get("Size")),
                ref_count=_int(usage.get("RefCount")),
                project=labels.get(COMPOSE_PROJECT_LABEL) == project,
                compose_volume=labels.get(COMPOSE_VOLUME_LABEL),
            )
        )
    # Unknown sizes sink to the bottom rather than reading as the smallest.
    items.sort(
        key=lambda v: (v.size_bytes is not None, v.size_bytes or 0), reverse=True
    )
    return VolumesOut(
        count=len(items),
        total_bytes=sum(v.size_bytes or 0 for v in items),
        project_bytes=sum(v.size_bytes or 0 for v in items if v.project),
        reclaimable_bytes=sum(v.size_bytes or 0 for v in items if v.ref_count == 0),
        items=items,
    )


def parse_build_cache(payload: dict[str, Any]) -> BuildCacheOut:
    records = payload.get("BuildCache") or []
    # Same rules as `docker system df`: a shared record is counted under the record
    # that owns it, and reclaimable is that total less what a build holds.
    total = sum(_int(r.get("Size")) or 0 for r in records if not r.get("Shared"))
    held = sum(
        _int(r.get("Size")) or 0
        for r in records
        if r.get("InUse") and not r.get("Shared")
    )
    return BuildCacheOut(
        count=len(records), total_bytes=total, reclaimable_bytes=max(total - held, 0)
    )


def parse_du(
    stdout: str, inside_root: str, host_root: str
) -> tuple[int | None, list[DirSizeOut]]:
    """`du -a -k -d 2` lines (`KiB<TAB>path`) to host paths, keeping the top level
    and the children of DEEP_DIRS. The size leads and the tab is du's own separator,
    so a name with spaces or tabs survives; a line that does not parse (the tail of
    a name with a newline in it) is skipped rather than guessed at."""
    total: int | None = None
    entries: list[DirSizeOut] = []
    host = host_root.rstrip("/")
    for line in stdout.splitlines():
        size, sep, path = line.partition("\t")
        if not sep or not size.isdigit():
            continue
        if path == inside_root:
            total = int(size) * 1024
            continue
        if not path.startswith(inside_root + "/"):
            continue
        rel = path[len(inside_root) + 1 :]
        parts = rel.split("/")
        if len(parts) > 2 or (len(parts) == 2 and parts[0] not in DEEP_DIRS):
            continue
        entries.append(DirSizeOut(path=f"{host}/{rel}", bytes=int(size) * 1024))
    # Ties by path, so a dir sits just above its only child of the same size.
    entries.sort(key=lambda e: (-e.bytes, e.path))
    return total, entries


def parse_statfs(stdout: str) -> dict[str, FilesystemOut]:
    """`stat -f -c '%n %i %S %b %f %a'` lines, keyed by the stat'ed path."""
    out: dict[str, FilesystemOut] = {}
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) != 6 or not all(f.isdigit() for f in fields[2:]):
            continue
        name, fsid = fields[0], fields[1]
        bsize, blocks, bfree, bavail = (int(f) for f in fields[2:])
        out[name] = FilesystemOut(
            paths=[],
            fsid=fsid,
            total_bytes=blocks * bsize,
            used_bytes=(blocks - bfree) * bsize,
            free_bytes=bavail * bsize,
        )
    return out


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _ERR_TAIL else "…" + text[-_ERR_TAIL:]


def _error(stage: str, exc: Exception) -> str:
    return f"{stage}: {type(exc).__name__}: {_tail(str(exc))}"


class DiskUsage:
    """Builds and caches the report. One build at a time: a second caller waits on
    the lock and then reads the fresh cache instead of starting another `du`."""

    def __init__(
        self,
        probe: DiskProbe,
        project: str,
        project_dir: str,
        *,
        ttl_s: float = CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
    ) -> None:
        self._probe = probe
        self._project = project
        self._project_dir = project_dir
        self._ttl_s = ttl_s
        self._clock = clock
        self._statvfs = statvfs
        self._lock = threading.Lock()
        self._cached: tuple[float, DiskReport] | None = None

    def report(self, *, refresh: bool = False) -> DiskReport:
        asked = self._clock()
        if not self._lock.acquire(blocking=False):
            # A build is running. Serve the last one now rather than queue behind it;
            # only a caller with nothing to serve waits.
            cached = self._cached
            if cached is not None:
                return self._served(cached, stale=True)
            self._lock.acquire()
        try:
            if self._cached is not None:
                at, _ = self._cached
                # Built after this request arrived (a caller it queued behind): as
                # fresh as a refresh could make it, so a burst of refreshes is one du.
                if at > asked:
                    return self._served(self._cached, stale=False)
                if not refresh and self._clock() - at < self._ttl_s:
                    return self._served(self._cached, stale=False)
            fresh = self._build()
            self._cached = (self._clock(), fresh)
            return fresh
        finally:
            self._lock.release()

    def _served(self, cached: tuple[float, DiskReport], *, stale: bool) -> DiskReport:
        at, report = cached
        age = round(self._clock() - at, 1)
        return report.model_copy(update={"cached": True, "stale": stale, "age_s": age})

    def _build(self) -> DiskReport:
        errors: list[str] = []
        docker_root: str | None = None
        try:
            docker_root = self._probe.docker_root_dir()
        except Exception as exc:
            errors.append(_error("docker info", exc))
        docker: DockerOut | None = None
        try:
            payload = self._probe.docker_df()
            docker = DockerOut(
                root_dir=docker_root,
                images=parse_images(payload),
                containers=parse_containers(payload, self._project),
                volumes=parse_volumes(payload, self._project),
                build_cache=parse_build_cache(payload),
            )
        except Exception as exc:
            errors.append(_error("docker df", exc))
        return DiskReport(
            generated_at=datetime.now(UTC).isoformat(),
            filesystem=self._filesystems(docker_root, errors),
            docker=docker,
            project_dirs=self._project_dirs(errors),
            errors=errors,
        )

    def _filesystems(
        self, docker_root: str | None, errors: list[str]
    ) -> list[FilesystemOut]:
        found: list[FilesystemOut] = []
        try:
            vfs = self._statvfs("/")
            found.append(
                FilesystemOut(
                    paths=["/ (supervisor)"],
                    fsid=None,
                    total_bytes=vfs.f_blocks * vfs.f_frsize,
                    used_bytes=(vfs.f_blocks - vfs.f_bfree) * vfs.f_frsize,
                    free_bytes=vfs.f_bavail * vfs.f_frsize,
                )
            )
        except OSError as exc:
            errors.append(_error("statvfs /", exc))
        mounts = {self._project_dir: PROJECT_MOUNT}
        labels = {PROJECT_MOUNT: f"project {self._project_dir}"}
        if docker_root:
            mounts[docker_root] = DOCKER_ROOT_MOUNT
            labels[DOCKER_ROOT_MOUNT] = f"docker data root {docker_root}"
        argv = ["stat", "-f", "-c", "%n %i %S %b %f %a", *labels]
        try:
            run = self._probe.run_readonly_helper(argv, mounts, STAT_TIMEOUT_S)
        except Exception as exc:
            errors.append(_error("stat helper", exc))
            return found
        stats = parse_statfs(run.stdout)
        if run.exit_code != 0 or len(stats) != len(labels):
            errors.append(
                f"stat helper: exit {run.exit_code}: {_tail(run.stderr) or 'no output'}"
            )
        # The data root first, so the supervisor's overlay `/` folds into it.
        for inside in sorted(labels, key=lambda m: m != DOCKER_ROOT_MOUNT):
            fs = stats.get(inside)
            if fs is None:
                continue
            twin = next(
                (
                    f
                    for f in found
                    if (f.fsid is not None and f.fsid == fs.fsid)
                    or (f.fsid is None and f.total_bytes == fs.total_bytes)
                ),
                None,
            )
            if twin is None:
                found.append(fs.model_copy(update={"paths": [labels[inside]]}))
            else:
                twin.paths.append(labels[inside])
                if twin.fsid is None:
                    # The helper's numbers are the real mount's; the overlay's are a
                    # mirror of them, so the folded entry takes the helper's.
                    twin.fsid = fs.fsid
                    twin.used_bytes, twin.free_bytes = fs.used_bytes, fs.free_bytes
        return found

    def _project_dirs(self, errors: list[str]) -> ProjectDirsOut | None:
        # -x: never cross into another filesystem mounted inside the tree. du never
        # follows symlinks unless asked (-L), so a link out of the tree is sized as
        # the link itself.
        argv = ["du", "-a", "-x", "-k", "-d", "2", PROJECT_MOUNT]
        try:
            run = self._probe.run_readonly_helper(
                argv, {self._project_dir: PROJECT_MOUNT}, DU_TIMEOUT_S
            )
        except Exception as exc:
            errors.append(_error("du helper", exc))
            return None
        if run.exit_code is None:
            errors.append(
                f"du helper: timed out after {DU_TIMEOUT_S:.0f} s; project_dirs is "
                "PARTIAL (only entries finished before the cap) and a retry hits the "
                "same cap"
            )
        total, entries = parse_du(run.stdout, PROJECT_MOUNT, self._project_dir)
        if run.exit_code not in (0, None):
            # du exits 1 on one unreadable entry yet still sizes everything else.
            errors.append(
                f"du helper: exit {run.exit_code}"
                f"{' (partial)' if entries else ''}: {_tail(run.stderr)}"
            )
        if total is None and not entries:
            return None
        return ProjectDirsOut(
            root=self._project_dir, total_bytes=total, entries=entries
        )
