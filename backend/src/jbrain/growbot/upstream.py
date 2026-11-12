"""Watch the official GrowBot repo for new robot-body policies (docs/plans/GROWBOT_PLAN.md §5).

The box serves a pinned, patched copy of upstream (`growbot/patches/upstream.json`), and
upstream occasionally publishes new trained walks and spins. Bringing one in is a sync PR
(`growbot/sync.sh` rebuilds the patched site around it), so the box cannot pull it in by
itself; what it can do is notice, tell the owner once, and show what changed.

Two requests per check at most, and usually one. The commits Atom feed (not API-limited)
says where upstream's main is; only when that has moved off the pin does one unauthenticated
`compare` call list the changed files. The box has no GitHub credential and the API allows
60 requests an hour per IP shared with everything else, so a compare already answered for
this (pin, head) pair is never asked again.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from pydantic import BaseModel, ValidationError

from jbrain.web.feeds import parse_feed

log = structlog.get_logger()

BRANCH = "main"
CHECK_EVERY = timedelta(hours=12)
# A tap on "Check now" inside this window answers from the last check: the owner cannot
# spend the box's share of the API limit by tapping.
MIN_RECHECK = timedelta(seconds=60)
MAX_COMMITS = 20
MAX_COMPARE_BYTES = 32_000_000
NOTIFY_KIND = "growbot_policies"

_REPO_RE = re.compile(r"^https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})/?$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_COMMIT_URL_RE = re.compile(r"/commit/([0-9a-f]{40})\b")

Change = Literal["new", "changed", "removed", "renamed"]
State = Literal["current", "behind", "unknown"]

_CHANGE: dict[str, Change] = {
    "added": "new",
    "modified": "changed",
    "changed": "changed",
    "copied": "new",
    "removed": "removed",
    "renamed": "renamed",
}


@dataclass(frozen=True)
class Pin:
    owner: str
    repo: str
    commit: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.slug}"


def read_pin(patches_dir: str | Path) -> Pin | None:
    """The upstream repo and commit the served copy was built from, or None when the mount
    is missing or the file is not the shape `sync.sh` writes."""
    try:
        raw = json.loads((Path(patches_dir) / "upstream.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    repo, commit = raw.get("repo"), raw.get("commit")
    if not isinstance(repo, str) or not isinstance(commit, str):
        return None
    m = _REPO_RE.match(repo)
    if m is None or not _SHA_RE.match(commit):
        return None
    return Pin(m.group(1), m.group(2).removesuffix(".git"), commit)


def is_policy_path(path: str) -> bool:
    """A trained body policy: a JSON weight file the app runs (`app/policies/…`,
    `app/policy*.json`) or one a contributor publishes under `policy/`. Only JSON, because
    that is the form the app loads; `policy/` also carries hundreds of training checkpoints,
    notebooks and videos, which are not something the robot can run."""
    if not path.endswith(".json"):
        return False
    if path.startswith(("app/policies/", "policy/")):
        return True
    return path.startswith("app/policy") and path.count("/") == 1


class UpstreamPolicy(BaseModel):
    path: str
    change: Change
    previous_path: str | None = None


class UpstreamCommit(BaseModel):
    sha: str
    title: str
    date: str = ""


class UpstreamStatus(BaseModel):
    state: State = "unknown"
    repo_url: str = ""
    pinned: str | None = None
    head: str | None = None
    head_at: str = ""
    # None: upstream has moved, but the file list could not be read this time.
    ahead_by: int | None = None
    commits: list[UpstreamCommit] = []
    policies: list[UpstreamPolicy] | None = None
    other_files: int | None = None
    compare_url: str = ""
    checked_at: datetime | None = None
    error: str = ""
    # The upstream head the owner was last told about, so each version notifies once.
    notified_head: str | None = None


class UpstreamUnavailable(Exception):
    """GitHub did not answer in a shape we can read."""


Fetch = Callable[[str], Awaitable[bytes]]


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0][:200] if message.strip() else ""


def _obj(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _parse_compare(raw: bytes) -> tuple[int, list[UpstreamCommit], list[UpstreamPolicy], int]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise UpstreamUnavailable("GitHub sent something that isn't JSON.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise UpstreamUnavailable("GitHub's answer had no file list.")
    ahead = data.get("ahead_by")
    commits: list[UpstreamCommit] = []
    for c in data.get("commits") or []:
        if not isinstance(c, dict):
            continue
        commit = _obj(c.get("commit"))
        committer = _obj(commit.get("committer"))
        commits.append(
            UpstreamCommit(
                sha=str(c.get("sha", ""))[:40],
                title=_first_line(str(commit.get("message", ""))),
                date=str(committer.get("date", ""))[:25],
            )
        )
    policies: list[UpstreamPolicy] = []
    other = 0
    for f in data["files"]:
        if not isinstance(f, dict) or not isinstance(f.get("filename"), str):
            continue
        name = f["filename"]
        prev = f.get("previous_filename") if isinstance(f.get("previous_filename"), str) else None
        if is_policy_path(name) or (prev is not None and is_policy_path(prev)):
            change = _CHANGE.get(str(f.get("status")), "changed")
            policies.append(UpstreamPolicy(path=name, change=change, previous_path=prev))
        else:
            other += 1
    # The compare API lists commits oldest first; the owner reads newest first.
    commits.reverse()
    return (ahead if isinstance(ahead, int) else len(commits)), commits, policies, other


class UpstreamWatch:
    """The check, its stored result, and the once-per-version notification. Everything it
    touches comes in as a callable, so the tests drive it with no network or database."""

    def __init__(
        self,
        *,
        pin: Callable[[], Pin | None],
        fetch_feed: Fetch,
        fetch_json: Fetch,
        load: Callable[[], Awaitable[object]],
        save: Callable[[dict[str, object]], Awaitable[None]],
        notify: Callable[[str, str], None],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pin = pin
        self._fetch_feed = fetch_feed
        self._fetch_json = fetch_json
        self._load = load
        self._save = save
        self._notify = notify
        self._now = now

    async def last(self) -> UpstreamStatus:
        raw = await self._load()
        if isinstance(raw, dict):
            try:
                return UpstreamStatus.model_validate(raw)
            except ValidationError:
                pass
        return UpstreamStatus()

    async def check(self, *, force: bool = False) -> UpstreamStatus:
        prev = await self.last()
        now = self._now()
        if prev.checked_at is not None:
            gap = now - prev.checked_at
            if gap < (MIN_RECHECK if force else CHECK_EVERY):
                return prev
        pin = self._pin()
        if pin is None:
            status = UpstreamStatus(
                checked_at=now,
                error="The box can't read which GrowBot version it serves. Ops → Update fixes "
                "a half-finished update.",
                notified_head=prev.notified_head,
            )
        else:
            status = await self._check(pin, prev, now)
        status = self._maybe_notify(status)
        await self._save(status.model_dump(mode="json"))
        return status

    async def _check(self, pin: Pin, prev: UpstreamStatus, now: datetime) -> UpstreamStatus:
        base = UpstreamStatus(
            repo_url=pin.url, pinned=pin.commit, checked_at=now, notified_head=prev.notified_head
        )
        try:
            items = parse_feed(await self._fetch_feed(f"{pin.url}/commits/{BRANCH}.atom"))
        except Exception as exc:  # noqa: BLE001 — any failure is "couldn't check", told to the owner
            log.warning("growbot.upstream.feed_failed", error=repr(exc))
            return self._failed(base, prev, "GitHub didn't answer. The box tries again later.")
        shas = [m.group(1) for it in items if (m := _COMMIT_URL_RE.search(it.url))]
        if not shas:
            return self._failed(base, prev, "GitHub's commit list for GrowBot was empty.")
        head = shas[0]
        base.head = head
        base.head_at = items[0].published
        if head == pin.commit:
            base.state = "current"
            base.ahead_by = 0
            base.policies = []
            base.other_files = 0
            return base
        base.state = "behind"
        base.compare_url = f"{pin.url}/compare/{pin.commit[:12]}...{head[:12]}"
        if prev.pinned == pin.commit and prev.head == head and prev.policies is not None:
            base.ahead_by = prev.ahead_by
            base.commits = prev.commits
            base.policies = prev.policies
            base.other_files = prev.other_files
            return base
        try:
            raw = await self._fetch_json(
                f"https://api.github.com/repos/{pin.slug}/compare/{pin.commit}...{head}"
            )
            ahead, commits, policies, other = _parse_compare(raw)
        except Exception as exc:  # noqa: BLE001 — the feed still says upstream moved
            log.warning("growbot.upstream.compare_failed", error=repr(exc))
            before_pin = shas.index(pin.commit) if pin.commit in shas else None
            base.ahead_by = before_pin
            base.commits = [
                UpstreamCommit(sha=s, title=_first_line(it.title), date=it.published)
                for s, it in zip(shas[: before_pin or MAX_COMMITS], items, strict=False)
            ]
            base.error = "Upstream has moved, but GitHub wouldn't list the changed files."
            return base
        base.ahead_by = ahead
        base.commits = commits[:MAX_COMMITS]
        base.policies = policies
        base.other_files = other
        return base

    @staticmethod
    def _failed(base: UpstreamStatus, prev: UpstreamStatus, error: str) -> UpstreamStatus:
        # A failed check keeps what the last good one found, so the card does not go blank.
        if prev.pinned == base.pinned:
            return prev.model_copy(update={"checked_at": base.checked_at, "error": error})
        base.error = error
        return base

    def _maybe_notify(self, status: UpstreamStatus) -> UpstreamStatus:
        if (
            status.state != "behind"
            or not status.policies
            or status.head is None
            or status.head == status.notified_head
        ):
            return status
        n = len(status.policies)
        names = ", ".join(_short(p.path) for p in status.policies[:3])
        more = f" and {n - 3} more" if n > 3 else ""
        self._notify(
            "New GrowBot walks and spins",
            f"Upstream published {n} new or changed body "
            f"{'policy' if n == 1 else 'policies'}: {names}{more}. "
            "Ask Claude to sync GrowBot, then Ops → Update.",
        )
        return status.model_copy(update={"notified_head": status.head})


def _short(path: str) -> str:
    for prefix in ("app/policies/", "app/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


async def run_upstream_loop(watch: UpstreamWatch, *, first_delay: float = 120.0) -> None:
    """Check on start-up (after the box settles) and then every few hours; `check` itself
    skips when the stored result is fresh, so restarts do not spend API calls."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            await watch.check()
        except Exception as exc:  # noqa: BLE001 — the watch must not kill the loop
            log.warning("growbot.upstream.check_error", error=repr(exc))
        await asyncio.sleep(3600.0)
