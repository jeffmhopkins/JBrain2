"""Job types read from the GrowBot_Training repo's ``jobs.toml``, so a new training job
needs a push to that repo and a Refresh, never a box update (owner, 2026-11-14).

The manifest only CHOOSES among things this file already allows: a job is a name, a
title, a config file under ``configs/`` and some display text. The shell it runs is the
fixed GrowBot pipeline below (install, smoke, ``./run.sh --config <config>``, package),
so the repo, the owner's Refresh and a debug token's Refresh can never put a command, a
URL or a branch on the box. Push access to that repo was already the trust boundary:
every GrowBot job has always run its ``run.sh`` from ``main``.

Each refresh pins the commit it read, and a job clones exactly that commit, so a run
records what it trained on. Starting a repo job refreshes first, so a config fix pushed
a minute ago is what runs. The last good catalog is kept on the work volume: a restart
or a failed fetch never empties the Jobs list, and a job dropped from the manifest stays
resolvable (retired) so its old runs' results and live folders still serve.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jlaunch_ctl import policy_index
from jlaunch_ctl import specs as specs_mod
from jlaunch_ctl.specs import JobSpec, Phase, SpecError, _validate

_log = logging.getLogger("jlaunch_ctl.catalog")

REPO_URL = "https://github.com/jeffmhopkins/GrowBot_Training"
BRANCH = "main"
MANIFEST = "jobs.toml"
CATEGORY = "Robot"

# Everything below lands in a ``bash -c`` phase or a filename, so it is shape-checked
# here and shell-quoted again by the runner. A config is a plain file in configs/.
# growbot_: the Body tab's policy import only offers runs of jobs named so.
_NAME_RE = re.compile(r"^growbot_[a-z0-9_]{1,56}$")
_CONFIG_RE = re.compile(r"^configs/[A-Za-z0-9_-]{1,64}\.toml$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_KEYS = {"name", "title", "config", "est_hours", "notes", "disk_gb"}
_REQUIRED = {"name", "title", "config"}
_MAX_JOBS = 50
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_TEXT = {"title": 120, "est_hours": 20, "notes": 1000}
_FETCH_TIMEOUT_S = 120.0
# A stalled connection gives up after this long under 1 KB/s, well inside the timeout.
_GIT_NET = ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=15"]


class CatalogError(RuntimeError):
    """A refresh failed. ``invalid`` means the manifest itself was rejected (the owner's
    to fix in the repo), as opposed to the fetch failing."""

    def __init__(self, message: str, *, invalid: bool = False) -> None:
        super().__init__(message)
        self.invalid = invalid


@dataclass(frozen=True)
class Snapshot:
    """What one fetch of the repo yields: the manifest text, the commit it came from,
    and the config files that commit holds (so a typo'd config fails the refresh, not a
    run an hour later)."""

    manifest: str
    commit: str
    configs: frozenset[str]
    # The kept policies (policy_index.py): the index's text ("" when the repo has none),
    # every plain file under policies/ as {path: (blob id, size)}, and a reader for one
    # blob's bytes, valid only while the fetch's scratch clone exists.
    policy_index: str = ""
    policy_blobs: dict[str, tuple[str, int]] = field(default_factory=dict)
    read_blob: Callable[[str], Awaitable[bytes]] | None = None


Fetcher = Callable[[Path], Awaitable[Snapshot]]


def _q(value: str) -> str:
    # Single-quoted for bash; the regexes above already exclude quotes.
    return "'" + value.replace("'", "'\\''") + "'"


def build_spec(entry: dict[str, object], commit: str) -> JobSpec:
    """The fixed GrowBot pipeline for one validated manifest entry."""
    name = str(entry["name"])
    config = str(entry["config"])
    artifact = f"{name}_artifacts.tar.gz"
    return _validate(
        JobSpec(
            name=name,
            title=str(entry["title"]),
            repo_url=REPO_URL,
            branch=BRANCH,
            phases=(
                Phase("install", "./install.sh"),
                Phase("smoke", ".venv/bin/python -m pytest -q"),
                Phase("train", f"./run.sh jlaunch --config {_q(config)}"),
                Phase("package", f"cp runs/jlaunch.tar.gz {_q(artifact)}"),
            ),
            artifact_path=artifact,
            artifact_media_type="application/gzip",
            verify_log="runs/jlaunch/export_verify.log",
            verify_must_end_with="EXPORT OK",
            headline_markers=("eval summary:", "max |json - network|:"),
            est_hours=str(entry.get("est_hours") or ""),
            disk_gb=int(entry.get("disk_gb") or 3),  # pyright: ignore[reportArgumentType]
            all_cores=True,
            notes=str(entry.get("notes") or ""),
            live_dir="runs/jlaunch/live",
            commit=commit,
            config=config,
            category=CATEGORY,
        )
    )


def parse_manifest(snapshot: Snapshot) -> list[dict[str, object]]:
    """Validate the whole manifest or reject it: one bad entry fails the refresh (and
    keeps the last good catalog) rather than half-applying."""
    if not _COMMIT_RE.fullmatch(snapshot.commit):
        raise CatalogError("the repo returned an odd commit id")
    if len(snapshot.manifest.encode()) > _MAX_MANIFEST_BYTES:
        raise CatalogError(f"{MANIFEST} is over 64 KB", invalid=True)
    try:
        data = tomllib.loads(snapshot.manifest)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"{MANIFEST} isn't valid TOML: {exc}", invalid=True) from exc
    if set(data) - {"job"}:
        extra = ", ".join(sorted(set(data) - {"job"}))
        raise CatalogError(f"{MANIFEST}: unknown top-level key {extra}", invalid=True)
    raw = data.get("job", [])
    if not isinstance(raw, list) or len(raw) > _MAX_JOBS:
        raise CatalogError(
            f"{MANIFEST}: [[job]] must be a list of at most {_MAX_JOBS}", invalid=True
        )
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw, 1):
        entries.append(_check_entry(item, i, seen, snapshot.configs))
    return entries


def _check_entry(
    item: object, i: int, seen: set[str], configs: frozenset[str] | None
) -> dict[str, object]:
    """One manifest entry, checked. ``configs`` is None when re-checking a saved entry,
    whose commit's tree isn't at hand; everything else is checked again."""

    def bad(why: str) -> CatalogError:
        return CatalogError(f"{MANIFEST} job {i}: {why}", invalid=True)

    if not isinstance(item, dict):
        raise bad("not a table")
    if set(item) - _KEYS:
        raise bad(f"unknown key {', '.join(sorted(set(item) - _KEYS))}")
    if _REQUIRED - set(item):
        raise bad(f"missing {', '.join(sorted(_REQUIRED - set(item)))}")
    for key in ("name", "title", "config", "est_hours", "notes"):
        if key in item and not isinstance(item[key], str):
            raise bad(f"{key} must be text")
    name, config = item["name"], item["config"]
    if not _NAME_RE.fullmatch(name):
        raise bad("name must be growbot_ then up to 56 of a-z, 0-9 and _")
    if name in seen:
        raise bad(f"{name} is listed twice")
    seen.add(name)
    if not _CONFIG_RE.fullmatch(config):
        raise bad("config must be configs/<name>.toml")
    if configs is not None and config not in configs:
        raise bad(f"{config} isn't in the repo")
    for key, limit in _MAX_TEXT.items():
        if len(str(item.get(key, ""))) > limit:
            raise bad(f"{key} is over {limit} characters")
    if not str(item["title"]).strip():
        raise bad("title is empty")
    disk = item.get("disk_gb", 3)
    if isinstance(disk, bool) or not isinstance(disk, int) or not 1 <= disk <= 100:
        raise bad("disk_gb must be a whole number from 1 to 100")
    return dict(item)


async def git_fetch(dest: Path) -> Snapshot:
    """Shallow-clone the repo's main into ``dest`` WITHOUT a checkout and read the
    manifest and the configs list from git's own objects. No working tree means no file
    on disk a symlink or a swap could stand in for, and a big push can't fill the disk
    with files nobody reads. Same transport guard as a job's own clone; no prompts, so a
    bad network fails fast."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    await _run(
        [
            "git",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=never",
            *_GIT_NET,
            "clone",
            "--quiet",
            "--no-checkout",
            "--depth",
            "1",
            "--branch",
            BRANCH,
            "--",
            REPO_URL,
            str(dest),
        ],
        env,
    )
    git = ["git", "-C", str(dest)]
    commit = (await _run([*git, "rev-parse", "HEAD"], env)).strip()
    # Only a plain file counts (mode 100644 blob): not a symlink, not a folder.
    listed = await _run(
        [*git, "ls-tree", "-z", commit, "--", MANIFEST, "configs/"], env
    )
    blobs = _plain_blobs(listed)
    if MANIFEST not in blobs:
        raise CatalogError(f"the repo has no {MANIFEST} on {BRANCH}", invalid=True)
    size = int((await _run([*git, "cat-file", "-s", blobs[MANIFEST]], env)).strip())
    if size > _MAX_MANIFEST_BYTES:
        raise CatalogError(f"{MANIFEST} is over 64 KB", invalid=True)
    manifest = await _run([*git, "cat-file", "blob", blobs[MANIFEST]], env)
    configs = frozenset(
        path for path in blobs if path.startswith("configs/") and path.count("/") == 1
    )
    kept = _plain_blobs_sized(
        await _run([*git, "ls-tree", "-r", "-l", "-z", commit, "--", "policies/"], env)
    )
    index = ""
    if policy_index.INDEX in kept:
        blob, isize = kept[policy_index.INDEX]
        if isize > policy_index.MAX_INDEX_BYTES:
            raise CatalogError(f"{policy_index.INDEX} is over 64 KB", invalid=True)
        index = await _run([*git, "cat-file", "blob", blob], env)

    async def read_blob(blob: str) -> bytes:
        return await _run_bytes([*git, "cat-file", "blob", blob], env)

    return Snapshot(
        manifest=manifest,
        commit=commit,
        configs=configs,
        policy_index=index,
        policy_blobs=kept,
        read_blob=read_blob,
    )


def _plain_blobs(ls_tree_z: str) -> dict[str, str]:
    """``git ls-tree -z`` output to {path: blob id}, regular files only."""
    out: dict[str, str] = {}
    for record in ls_tree_z.split("\0"):
        meta, _, path = record.partition("\t")
        parts = meta.split()
        # 100755 too: an executable bit is no reason to call a config missing.
        if len(parts) == 3 and parts[0] in ("100644", "100755") and parts[1] == "blob":
            out[path] = parts[2]
    return out


def _plain_blobs_sized(ls_tree_lz: str) -> dict[str, tuple[str, int]]:
    """``git ls-tree -l -z`` output to {path: (blob id, size)}, regular files only."""
    out: dict[str, tuple[str, int]] = {}
    for record in ls_tree_lz.split("\0"):
        meta, _, path = record.partition("\t")
        parts = meta.split()
        if len(parts) == 4 and parts[0] in ("100644", "100755") and parts[1] == "blob":
            out[path] = (parts[2], int(parts[3]))
    return out


async def _run(argv: list[str], env: dict[str, str]) -> str:
    return (await _run_bytes(argv, env)).decode("utf-8", errors="replace")


async def _run_bytes(argv: list[str], env: dict[str, str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _FETCH_TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError) as exc:
        # Cancelled too (shutdown): never leave a git child running or unreaped.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await asyncio.shield(proc.wait())
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise CatalogError("fetching the training repo timed out") from exc
    if proc.returncode != 0:
        tail = err.decode("utf-8", errors="replace").strip()[-300:]
        raise CatalogError(f"fetching the training repo failed: {tail}")
    return out


class Catalog:
    """The repo's job types, as last fetched. Publishes them into ``specs.REPO_SPECS``
    (listed and retired) so every spec lookup in the manager sees them."""

    def __init__(self, state_root: str, *, fetch: Fetcher = git_fetch) -> None:
        self._file = Path(state_root) / "catalog.json"
        # The kept policies' bytes, one file per git blob id.
        self._policy_dir = Path(state_root) / "policies"
        self._fetch = fetch
        self._lock = asyncio.Lock()
        # A refresh already under way: a second caller shares its answer rather than
        # queueing another clone behind it.
        self._inflight: asyncio.Future[dict[str, object]] | None = None
        self.commit = ""
        self.fetched_at: str | None = None
        self.error: str | None = None
        self._listed: dict[str, dict[str, object]] = {}
        # Every entry ever listed, by name, with the commit it was last listed at — a
        # retired job still resolves so its runs keep their results.
        self._known: dict[str, tuple[dict[str, object], str]] = {}
        # The index's kept policies, by slug, in index order.
        self._policies: dict[str, dict[str, object]] = {}

    def owns(self, name: str) -> bool:
        return name in self._listed

    def status(self) -> dict[str, object]:
        return {
            "repo": REPO_URL,
            "branch": BRANCH,
            "manifest": MANIFEST,
            "commit": self.commit,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "jobs": sorted(self._listed),
            "policies": list(self._policies),
        }

    def policies(self) -> list[dict[str, object]]:
        """The kept policies the index lists, with the commit they were read at."""
        return [{**e, "commit": self.commit} for e in self._policies.values()]

    def policy_bytes(self, slug: str) -> bytes:
        """One kept policy's JSON, served only if it still is the blob the index named:
        the work volume is writable by a running job."""
        entry = self._policies.get(slug)
        if entry is None:
            raise CatalogError(f"no kept policy {slug}")
        blob = str(entry["blob"])
        try:
            data = (self._policy_dir / f"{blob}.json").read_bytes()
        except OSError as exc:
            raise CatalogError(f"kept policy {slug} isn't on the box") from exc
        if policy_index.blob_id(data) != blob:
            raise CatalogError(f"kept policy {slug} doesn't match the repo")
        return data

    def load(self) -> None:
        """Restore the last good catalog from the work volume. A missing or damaged file
        leaves it empty until the next refresh (never a server that won't start), and an
        entry that fails a fresh entry's checks is dropped rather than trusted."""
        try:
            data = json.loads(self._file.read_text())
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            self._publish()
            return
        known: dict[str, tuple[dict[str, object], str]] = {}
        saved = data.get("known")
        for name, rec in (saved if isinstance(saved, dict) else {}).items():
            with contextlib.suppress(
                CatalogError, SpecError, KeyError, TypeError, ValueError
            ):
                # The work volume is writable by a running job, so a saved entry gets
                # every check a fresh one does except the config's presence.
                entry = _check_entry(rec["entry"], 0, set(), None)
                commit = rec["commit"]
                if name == entry["name"] and isinstance(commit, str):
                    build_spec(entry, commit)
                    known[name] = (entry, commit)
        listed = data.get("listed")
        names = (
            [n for n in listed if isinstance(n, str)]
            if isinstance(listed, list)
            else []
        )
        commit, fetched_at = data.get("commit"), data.get("fetched_at")
        self._known = known
        self._listed = {n: known[n][0] for n in names if n in known}
        self.commit = commit if isinstance(commit, str) else ""
        self.fetched_at = fetched_at if isinstance(fetched_at, str) else None
        self._policies = {}
        saved_policies = data.get("policies")
        for item in saved_policies if isinstance(saved_policies, list) else []:
            with contextlib.suppress(policy_index.PolicyIndexError):
                entry = policy_index.check_entry(item, 0)
                if "blob" in entry:
                    self._policies[str(entry["slug"])] = entry
        self._publish()

    async def refresh(self) -> dict[str, object]:
        """Fetch the manifest and swap it in whole. On any failure the previous catalog
        stays and the error is recorded and raised."""
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.ensure_future(self._refresh())
        return await asyncio.shield(self._inflight)

    async def _refresh(self) -> dict[str, object]:
        async with self._lock:
            # Off the work volume: a running job can write there, and must not be able
            # to stand in for the repo's files mid-read.
            before = set(self._listed)
            try:
                snapshot, entries, kept = await self._read()
                listed = {str(e["name"]): e for e in entries}
                known = {
                    **self._known,
                    **{n: (e, snapshot.commit) for n, e in listed.items()},
                }
                fetched_at = datetime.now(UTC).isoformat()
                # Saved before it's swapped in: a full disk leaves memory and the file
                # agreeing on the old list.
                policies = {str(e["slug"]): e for e in kept}
                self._save(snapshot.commit, fetched_at, listed, known, policies)
            except (CatalogError, OSError, ValueError) as exc:
                err = exc if isinstance(exc, CatalogError) else CatalogError(str(exc))
                self.error = str(err)
                _log.warning("catalog refresh failed: %s", err)
                raise err from exc
            self._listed, self._known, self._policies = listed, known, policies
            self.commit, self.fetched_at, self.error = snapshot.commit, fetched_at, None
            self._publish()
            self._prune_policy_files()
            after = set(listed)
            _log.info(
                "catalog refresh commit=%s jobs=%d", snapshot.commit[:12], len(after)
            )
            return {
                **self.status(),
                "added": sorted(after - before),
                "removed": sorted(before - after),
            }

    async def _read(
        self,
    ) -> tuple[Snapshot, list[dict[str, object]], list[dict[str, object]]]:
        # Off the work volume: a running job can write there, and must not be able to
        # stand in for the repo's files mid-read.
        scratch = Path(tempfile.mkdtemp(prefix="jlaunch-catalog-"))
        try:
            snapshot = await self._fetch(scratch / "repo")
            jobs = parse_manifest(snapshot)
            try:
                kept = policy_index.parse(snapshot.policy_index, snapshot.policy_blobs)
            except policy_index.PolicyIndexError as exc:
                raise CatalogError(str(exc), invalid=True) from exc
            for entry in kept:
                await self._store_policy(snapshot, str(entry["blob"]))
            return snapshot, jobs, kept
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    async def _store_policy(self, snapshot: Snapshot, blob: str) -> None:
        dest = self._policy_dir / f"{blob}.json"
        with contextlib.suppress(OSError):
            if policy_index.blob_id(dest.read_bytes()) == blob:
                return
        if snapshot.read_blob is None:
            raise CatalogError("the fetch can't read kept policies")
        data = await snapshot.read_blob(blob)
        if policy_index.blob_id(data) != blob:
            raise CatalogError("a kept policy didn't read back as the repo's file")
        self._policy_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)

    def _prune_policy_files(self) -> None:
        keep = {f"{e['blob']}.json" for e in self._policies.values()}
        with contextlib.suppress(OSError):
            for path in self._policy_dir.iterdir():
                if path.name not in keep:
                    path.unlink(missing_ok=True)

    async def refresh_quietly(self) -> None:
        """Refresh if the repo answers; otherwise run from the last good catalog."""
        with contextlib.suppress(CatalogError):
            await self.refresh()

    def _publish(self) -> None:
        specs_mod.REPO_SPECS.clear()
        for name, (entry, commit) in self._known.items():
            specs_mod.REPO_SPECS[name] = build_spec(entry, commit)
        specs_mod.LISTED_REPO_SPECS.clear()
        specs_mod.LISTED_REPO_SPECS.extend(sorted(self._listed))

    def _save(
        self,
        commit: str,
        fetched_at: str,
        listed: dict[str, dict[str, object]],
        known: dict[str, tuple[dict[str, object], str]],
        policies: dict[str, dict[str, object]],
    ) -> None:
        data = {
            "commit": commit,
            "fetched_at": fetched_at,
            "listed": sorted(listed),
            "known": {n: {"entry": e, "commit": c} for n, (e, c) in known.items()},
            "policies": list(policies.values()),
        }
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self._file)
