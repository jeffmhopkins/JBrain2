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
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jlaunch_ctl import specs as specs_mod
from jlaunch_ctl.specs import JobSpec, Phase, SpecError, _validate

_log = logging.getLogger("jlaunch_ctl.catalog")

REPO_URL = "https://github.com/jeffmhopkins/GrowBot_Training"
BRANCH = "main"
MANIFEST = "jobs.toml"
CATEGORY = "Robot"

# Everything below lands in a ``bash -c`` phase or a filename, so it is shape-checked
# here and shell-quoted again by the runner. A config is a plain file in configs/.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_CONFIG_RE = re.compile(r"^configs/[A-Za-z0-9_-]{1,64}\.toml$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_KEYS = {"name", "title", "config", "est_hours", "notes", "disk_gb"}
_REQUIRED = {"name", "title", "config"}
_MAX_JOBS = 50
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_TEXT = {"title": 120, "est_hours": 20, "notes": 1000}
_FETCH_TIMEOUT_S = 120.0


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
    item: object, i: int, seen: set[str], configs: frozenset[str]
) -> dict[str, object]:
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
        raise bad("name must be 3-64 of a-z, 0-9 and _, starting with a letter")
    if name in specs_mod.SPECS:
        raise bad(f"{name} is a built-in job's name")
    if name in seen:
        raise bad(f"{name} is listed twice")
    seen.add(name)
    if not _CONFIG_RE.fullmatch(config):
        raise bad("config must be configs/<name>.toml")
    if config not in configs:
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
    """Shallow-clone the repo's main into ``dest`` and read the manifest from it. Same
    transport guard as a job's own clone; no prompts, so a bad network fails fast."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    await _run(
        [
            "git",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.file.allow=never",
            "clone",
            "--quiet",
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
    commit = (await _run(["git", "-C", str(dest), "rev-parse", "HEAD"], env)).strip()
    manifest_path = dest / MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise CatalogError(f"the repo has no {MANIFEST} on {BRANCH}", invalid=True)
    with manifest_path.open("rb") as fh:
        manifest = fh.read(_MAX_MANIFEST_BYTES + 1).decode("utf-8", errors="replace")
    configs = frozenset(
        f"configs/{p.name}"
        for p in (dest / "configs").glob("*.toml")
        if p.is_file() and not p.is_symlink()
    )
    return Snapshot(manifest=manifest, commit=commit, configs=configs)


async def _run(argv: list[str], env: dict[str, str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _FETCH_TIMEOUT_S)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise CatalogError("fetching the training repo timed out") from exc
    if proc.returncode != 0:
        tail = err.decode("utf-8", errors="replace").strip()[-300:]
        raise CatalogError(f"fetching the training repo failed: {tail}")
    return out.decode("utf-8", errors="replace")


class Catalog:
    """The repo's job types, as last fetched. Publishes them into ``specs.REPO_SPECS``
    (listed and retired) so every spec lookup in the manager sees them."""

    def __init__(self, state_root: str, *, fetch: Fetcher = git_fetch) -> None:
        self._file = Path(state_root) / "catalog.json"
        self._scratch = Path(state_root) / ".catalog-fetch"
        self._fetch = fetch
        self._lock = asyncio.Lock()
        self.commit = ""
        self.fetched_at: str | None = None
        self.error: str | None = None
        self._listed: dict[str, dict[str, object]] = {}
        # Every entry ever listed, by name, with the commit it was last listed at — a
        # retired job still resolves so its runs keep their results.
        self._known: dict[str, tuple[dict[str, object], str]] = {}

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
        }

    def load(self) -> None:
        """Restore the last good catalog from the work volume. A missing or damaged file
        leaves it empty until the next refresh, and a stale entry that no longer passes
        today's checks is dropped rather than trusted."""
        with contextlib.suppress(OSError, json.JSONDecodeError, TypeError, ValueError):
            data = json.loads(self._file.read_text())
            known = {}
            for name, rec in dict(data.get("known", {})).items():
                with contextlib.suppress(SpecError, KeyError, TypeError, ValueError):
                    entry, commit = dict(rec["entry"]), str(rec["commit"])
                    if _NAME_RE.fullmatch(name) and name == entry["name"]:
                        build_spec(entry, commit)
                        known[name] = (entry, commit)
            self._known = known
            self._listed = {
                n: known[n][0] for n in data.get("listed", []) if n in known
            }
            self.commit = str(data.get("commit", ""))
            self.fetched_at = data.get("fetched_at")
        self._publish()

    async def refresh(self) -> dict[str, object]:
        """Fetch the manifest and swap it in whole. On any failure the previous catalog
        stays and the error is recorded and raised."""
        async with self._lock:
            try:
                snapshot = await self._fetch(self._scratch)
                entries = parse_manifest(snapshot)
            except CatalogError as exc:
                self.error = str(exc)
                _log.warning("catalog refresh failed: %s", exc)
                raise
            finally:
                shutil.rmtree(self._scratch, ignore_errors=True)
            before = set(self._listed)
            self._listed = {str(e["name"]): e for e in entries}
            for name, entry in self._listed.items():
                self._known[name] = (entry, snapshot.commit)
            self.commit = snapshot.commit
            self.fetched_at = datetime.now(UTC).isoformat()
            self.error = None
            self._publish()
            self._save()
            after = set(self._listed)
            _log.info(
                "catalog refresh commit=%s jobs=%d", snapshot.commit[:12], len(after)
            )
            return {
                **self.status(),
                "added": sorted(after - before),
                "removed": sorted(before - after),
            }

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

    def _save(self) -> None:
        data = {
            "commit": self.commit,
            "fetched_at": self.fetched_at,
            "listed": sorted(self._listed),
            "known": {
                n: {"entry": e, "commit": c} for n, (e, c) in self._known.items()
            },
        }
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self._file)
