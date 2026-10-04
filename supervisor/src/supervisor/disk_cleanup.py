"""Owner-triggered disk cleanup: the reclaimable parts of `docker system df`, freed.

Three actions, each narrow on purpose, and a dry run (the default) that reports what
each WOULD free without touching anything:

- `build_cache`: `docker builder prune --all` — every cache record no build holds.
  The update keeps 10 GB of it; this clears that too, at the cost of a slower next
  build.
- `unused_images`: images no container uses, minus every image the stack could need:
  any repository the deploy compose file names (`image:` lines, and image-shaped
  `${VAR:-default}` values such as the build-base args, with the box's `.env`
  overrides of those variables), every `FROM` / image-shaped `ARG` default in the
  source tree's Dockerfiles, the `jbrain2-*` / `<project>-*` builds and the helper
  images the one-shots and backups pull. All of it is read from the box on every run;
  when any of it cannot be read, no image is removed.
- `orphan_volumes`: ONLY volumes named in ORPHAN_VOLUME_ALLOWLIST, and only while no
  container references them. Never a volume holding owner data.

Removals are never forced: the daemon's own in-use refusal stays a second guard.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel

from supervisor.disk_usage import parse_build_cache

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from supervisor.gateway import HelperRun

CleanupAction = Literal["build_cache", "unused_images", "orphan_volumes"]
ACTION_ORDER: tuple[CleanupAction, ...] = (
    "build_cache",
    "unused_images",
    "orphan_volumes",
)

# Leftovers of earlier designs, named one by one. `jbrain_llm_kv` held the v1 prompt
# cache before it moved to `local-models/.kvslots`; nothing in the compose file or the
# code references it now. A name is added here only after the same check — this list
# is the whole of what `orphan_volumes` may ever remove.
ORPHAN_VOLUME_ALLOWLIST = frozenset({"jbrain_llm_kv"})

# Pulled by the supervisor's one-shots (docker:cli) and by backup/restore (alpine)
# rather than named in the compose file, so the compose read alone would miss them.
ALWAYS_KEEP_REPOS = frozenset({"docker", "alpine"})
# The stack's own builds: `jbrain2-*:local` by name, and `<project>-<service>` for a
# service with `build:` and no `image:` (compose's default name). A service on a profile
# that is not running has an image but no container, so "unused" alone would remove it.
BUILT_IMAGE_PREFIX = "jbrain2-"

COMPOSE_FILE = "docker-compose.yml"
COMPOSE_MOUNT = "/mnt/compose.yml"
ENV_MOUNT = "/mnt/env"
SRC_MOUNT = "/mnt/src"
READ_TIMEOUT_S = 20.0

_IMAGE_LINE = re.compile(r"^\s*image:\s*(\S+)\s*(?:#.*)?$", re.M)
# ${VAR}, ${VAR:-default}, ${VAR-default}.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")
# A value that reads as an image reference (registry/repo, tag, digest) and not a URL
# or a word: what decides that a `${VAR:-default}` names an image.
_IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:[/:@][A-Za-z0-9._:@/-]+)+$")
_DOCKERFILE_LINE = re.compile(
    r"^\s*(FROM|ARG)\s+(?:--platform=\S+\s+)?([^\s=]+)(?:=(\S+))?", re.I
)


class CleanupProbe(Protocol):
    """The docker calls cleanup needs; ComposeDockerGateway implements it."""

    def docker_df(self) -> dict[str, Any]: ...

    def run_readonly_helper(
        self, argv: Sequence[str], mounts: Mapping[str, str], timeout_s: float
    ) -> HelperRun: ...

    def prune_build_cache(self) -> int: ...

    def remove_image(self, ref: str) -> None: ...

    def remove_volume(self, name: str) -> None: ...


class CleanupBusyError(RuntimeError):
    """Another cleanup is running."""


class CleanupItem(BaseModel):
    name: str
    # Unique bytes for an image, the volume's size; None when the daemon did not
    # size it.
    bytes: int | None


class KeptItem(BaseModel):
    name: str
    reason: str


class ActionResult(BaseModel):
    action: CleanupAction
    dry_run: bool
    # What the action WOULD free (dry run) or did free. For images it is the sum of
    # the removed images' unique bytes, so layers shared with a kept image never count.
    bytes: int
    items: list[CleanupItem]
    kept: list[KeptItem]
    errors: list[str]


class CleanupResult(BaseModel):
    dry_run: bool
    total_bytes: int
    actions: list[ActionResult]


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def repo_of(ref: str) -> str:
    """The repository an image reference names, tag and digest dropped and Docker
    Hub's implicit prefixes folded, so `docker.io/library/alpine:3` and the daemon's
    `alpine:latest` compare equal."""
    name = ref.split("@", 1)[0]
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        name = name[:colon]
    for prefix in ("docker.io/", "index.docker.io/"):
        name = name.removeprefix(prefix)
    return name.removeprefix("library/")


def _image_shaped(value: str) -> bool:
    return "://" not in value and _IMAGE_REF.match(value) is not None


def image_variables(text: str) -> set[str]:
    """Compose variables that can name an image: those on `image:` lines and those
    whose default is image-shaped (the build-base args). Only these are read from the
    box's .env — never its secrets."""
    names: set[str] = set()
    for raw in _IMAGE_LINE.findall(text):
        names.update(m.group(1) for m in _VAR.finditer(raw))
    for match in _VAR.finditer(text):
        default = (match.group(2) or "").strip("'\"")
        if default and _image_shaped(default):
            names.add(match.group(1))
    return names


def parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name and not name.startswith("#"):
            env[name.removeprefix("export ").strip()] = value.strip().strip("'\"")
    return env


def compose_image_repos(text: str, env: Mapping[str, str]) -> set[str] | None:
    """Every repository the compose file can run or build from, with the box's .env
    applied; None when an `image:` line resolves to nothing (no default, no
    override) — the caller then keeps every image."""
    repos: set[str] = set()
    for raw in _IMAGE_LINE.findall(text):
        value = raw.strip("'\"")
        match = _VAR.fullmatch(value)
        if match is not None:
            value = env.get(match.group(1)) or match.group(2) or ""
        elif "$" in value:
            return None
        if not value:
            return None
        repos.add(repo_of(value))
    if not repos:
        return None
    # Build args and anything else image-shaped: both the default and the override,
    # since a stale override and its default can both be on disk. Keeping more is
    # the safe direction.
    for match in _VAR.finditer(text):
        for value in (match.group(2) or "", env.get(match.group(1), "")):
            value = value.strip("'\"")
            if value and _image_shaped(value):
                repos.add(repo_of(value))
    return repos


def dockerfile_repos(text: str) -> set[str]:
    """`FROM` images and image-shaped `ARG` defaults of `grep`ped Dockerfile lines.
    A `FROM ${ARG}` is covered by that ARG's default; a stage name read as a repo
    only keeps more."""
    repos: set[str] = set()
    for line in text.splitlines():
        match = _DOCKERFILE_LINE.match(line)
        if match is None:
            continue
        kind, word, value = match.group(1).upper(), match.group(2), match.group(3)
        ref = word if kind == "FROM" else (value or "")
        ref = ref.strip("'\"")
        if ref and "$" not in ref and (kind == "FROM" or _image_shaped(ref)):
            repos.add(repo_of(ref))
    return repos


def _tags(raw: Mapping[str, Any]) -> list[str]:
    return [t for t in (raw.get("RepoTags") or []) if t and t != "<none>:<none>"]


class DiskCleanup:
    """Runs cleanup actions one request at a time; `on_applied` runs after anything
    was actually removed (the disk report's cache invalidation)."""

    def __init__(
        self,
        probe: CleanupProbe,
        project: str,
        project_dir: str,
        *,
        on_applied: Callable[[], None] | None = None,
    ) -> None:
        self._probe = probe
        self._built_prefixes = (BUILT_IMAGE_PREFIX, f"{project}-")
        self._project_dir = project_dir.rstrip("/")
        self._on_applied = on_applied
        self._lock = threading.Lock()
        self._applying = False

    @property
    def applying(self) -> bool:
        """True from reserve_apply() until that cleanup ends: the one-shot routes
        refuse to start while it holds, as this refuses to apply under a one-shot."""
        return self._applying

    def reserve_apply(self) -> bool:
        """Claim the one cleanup slot for an apply, or False when one runs. The app
        calls this under the same lock its one-shot starts take, so a one-shot and an
        apply can never both begin; run_reserved() then frees it."""
        if not self._lock.acquire(blocking=False):
            return False
        self._applying = True
        return True

    def run_reserved(self, actions: Iterable[CleanupAction]) -> CleanupResult:
        try:
            return self._result(actions, dry_run=False)
        finally:
            self._applying = False
            self._lock.release()

    def run(self, actions: Iterable[CleanupAction], *, dry_run: bool) -> CleanupResult:
        if not dry_run:
            if not self.reserve_apply():
                raise CleanupBusyError
            return self.run_reserved(actions)
        if not self._lock.acquire(blocking=False):
            raise CleanupBusyError
        try:
            return self._result(actions, dry_run=True)
        finally:
            self._lock.release()

    def _result(
        self, actions: Iterable[CleanupAction], *, dry_run: bool
    ) -> CleanupResult:
        wanted = set(actions)
        results = self._run([a for a in ACTION_ORDER if a in wanted], dry_run=dry_run)
        return CleanupResult(
            dry_run=dry_run,
            total_bytes=sum(r.bytes for r in results),
            actions=results,
        )

    def _run(
        self, actions: list[CleanupAction], *, dry_run: bool
    ) -> list[ActionResult]:
        try:
            df = self._probe.docker_df()
        except Exception as exc:
            message = f"docker df: {type(exc).__name__}: {exc}"
            return [
                ActionResult(
                    action=a,
                    dry_run=dry_run,
                    bytes=0,
                    items=[],
                    kept=[],
                    errors=[message],
                )
                for a in actions
            ]
        steps: dict[CleanupAction, Callable[[dict[str, Any], bool], ActionResult]] = {
            "build_cache": self._build_cache,
            "unused_images": self._unused_images,
            "orphan_volumes": self._orphan_volumes,
        }
        try:
            return [steps[a](df, dry_run) for a in actions]
        finally:
            if not dry_run and self._on_applied is not None:
                self._on_applied()

    def _build_cache(self, df: dict[str, Any], dry_run: bool) -> ActionResult:
        cache = parse_build_cache(df)
        result = ActionResult(
            action="build_cache",
            dry_run=dry_run,
            bytes=cache.reclaimable_bytes,
            items=[
                CleanupItem(
                    name=f"build cache ({cache.count} records)",
                    bytes=cache.reclaimable_bytes,
                )
            ],
            kept=[],
            errors=[],
        )
        if dry_run:
            return result
        try:
            result.bytes = self._probe.prune_build_cache()
        except Exception as exc:
            result.bytes = 0
            result.errors.append(f"builder prune: {type(exc).__name__}: {exc}")
        return result

    def _read(
        self, argv: list[str], host: str, inside: str, ok: tuple[int, ...] = (0,)
    ) -> tuple[str | None, str | None]:
        """One file read through the read-only helper: (stdout, None) or (None, why).
        A missing source reads as None/"missing"."""
        try:
            run = self._probe.run_readonly_helper(argv, {host: inside}, READ_TIMEOUT_S)
        except Exception as exc:
            if "does not exist" in str(exc):
                return None, "missing"
            return None, f"{type(exc).__name__}: {exc}"
        if run.exit_code not in ok:
            return None, f"exit {run.exit_code}: {run.stderr.strip()[:300]}"
        return run.stdout, None

    def _protected_repos(self) -> tuple[set[str] | None, str | None]:
        root = self._project_dir
        compose, why = self._read(
            ["cat", COMPOSE_MOUNT], f"{root}/{COMPOSE_FILE}", COMPOSE_MOUNT
        )
        if compose is None:
            return None, f"compose file {root}/{COMPOSE_FILE}: {why}"
        env: dict[str, str] = {}
        names = sorted(image_variables(compose))
        if names:
            # Only the image variables' lines leave the helper; grep exits 1 when
            # none is overridden, and a box with no .env runs the defaults.
            pattern = f"^[[:space:]]*(export[[:space:]]+)?({'|'.join(names)})="
            out, why = self._read(
                ["grep", "-E", pattern, ENV_MOUNT], f"{root}/.env", ENV_MOUNT, (0, 1)
            )
            if out is None and why != "missing":
                return None, f"{root}/.env: {why}"
            env = parse_env(out or "")
        repos = compose_image_repos(compose, env)
        if repos is None:
            return None, "an image: line resolves to nothing"
        lines, why = self._read(
            [
                "grep",
                "-rhE",
                "--include=Dockerfile*",
                "--exclude-dir=.git",
                "--exclude-dir=node_modules",
                r"^[[:space:]]*(FROM|ARG)[[:space:]]",
                SRC_MOUNT,
            ],
            f"{root}/src",
            SRC_MOUNT,
        )
        if lines is None:
            return None, f"Dockerfiles under {root}/src: {why}"
        return repos | dockerfile_repos(lines), None

    def _unused_images(self, df: dict[str, Any], dry_run: bool) -> ActionResult:
        result = ActionResult(
            action="unused_images",
            dry_run=dry_run,
            bytes=0,
            items=[],
            kept=[],
            errors=[],
        )
        stack, why = self._protected_repos()
        if stack is None:
            # When in doubt, keep: without the full list, an image a stopped profile
            # service or the next build needs is indistinguishable from garbage.
            result.errors.append(f"{why}; no image removed")
            return result
        protected = stack | ALWAYS_KEEP_REPOS
        used_ids = {str(c.get("ImageID", "")) for c in df.get("Containers") or [] if c}
        for raw in df.get("Images") or []:
            image_id = str(raw.get("Id", ""))
            containers = _int(raw.get("Containers"))
            # Uncomputed (-1) reads as in use, as on the report.
            if containers is None or containers > 0 or image_id in used_ids:
                continue
            tags = _tags(raw)
            label = ", ".join(tags) or image_id.removeprefix("sha256:")[:12]
            repos = {repo_of(r) for r in tags + list(raw.get("RepoDigests") or [])}
            if any(r in protected or r.startswith(self._built_prefixes) for r in repos):
                result.kept.append(KeptItem(name=label, reason="compose/stack image"))
                continue
            shared = _int(raw.get("SharedSize"))
            if shared is None:
                # The daemon did not say what it shares, so neither what removing it
                # frees nor whether a kept image needs its layers: keep.
                result.kept.append(KeptItem(name=label, reason="shared size unknown"))
                continue
            unique = max((_int(raw.get("Size")) or 0) - shared, 0)
            if unique == 0:
                # Every layer is shared with a kept image: removing it frees nothing
                # and only costs a re-pull if it is a build's base.
                result.kept.append(KeptItem(name=label, reason="frees nothing"))
                continue
            if not dry_run:
                # One tag (or none) goes by id; several by tag, since an unforced
                # `rmi <id>` refuses an image referenced from more than one repo.
                refs = tags if len(tags) > 1 else [image_id]
                try:
                    for ref in refs:
                        self._probe.remove_image(ref)
                except Exception as exc:
                    result.errors.append(f"{label}: {type(exc).__name__}: {exc}")
                    continue
            result.items.append(CleanupItem(name=label, bytes=unique))
            result.bytes += unique
        return result

    def _orphan_volumes(self, df: dict[str, Any], dry_run: bool) -> ActionResult:
        result = ActionResult(
            action="orphan_volumes",
            dry_run=dry_run,
            bytes=0,
            items=[],
            kept=[],
            errors=[],
        )
        for raw in df.get("Volumes") or []:
            name = str(raw.get("Name", ""))
            if name not in ORPHAN_VOLUME_ALLOWLIST:
                continue
            usage = raw.get("UsageData") or {}
            refs = _int(usage.get("RefCount"))
            if refs != 0:
                reason = "ref count unknown" if refs is None else f"in use ({refs})"
                result.kept.append(KeptItem(name=name, reason=reason))
                continue
            size = _int(usage.get("Size"))
            if not dry_run:
                try:
                    self._probe.remove_volume(name)
                except Exception as exc:
                    result.errors.append(f"{name}: {type(exc).__name__}: {exc}")
                    continue
            result.items.append(CleanupItem(name=name, bytes=size))
            result.bytes += size or 0
        return result
