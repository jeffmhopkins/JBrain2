"""The kept GrowBot policies, read from the training repo's ``policies/index.toml`` with
the same fetch as ``jobs.toml`` (owner, 2026-11-14: "finished policies should get saved
to our training repo, and the growbot GUI ... should pull from that on a refresh").

The index names which kept policies the Body tab offers; the files themselves are the
policy JSONs ``scripts/keep_policy.sh`` copies under ``policies/<slug>/``. The launcher
only reads them out of git and serves the bytes: the api parses each one with the same
checks as a policy added from a finished job before it reaches the library, and a stored
copy is served only if it still hashes to the git blob id the index pointed at.
"""

from __future__ import annotations

import hashlib
import re
import tomllib

INDEX = "policies/index.toml"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_JOB_RE = re.compile(r"^growbot_[a-z0-9_]{1,56}$")
_FILE_RE = re.compile(r"^policies/[a-z0-9][a-z0-9_-]{0,47}/[A-Za-z0-9_-]{1,64}\.json$")
_RUN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BLOB_RE = re.compile(r"^[0-9a-f]{40}$")
_KEYS = {"slug", "title", "job", "file", "run", "notes"}
_REQUIRED = {"slug", "title", "job", "file"}
# The library's own limits (api growbot/policies.py: 60-character names, 4 MB files).
_MAX_TEXT = {"title": 60, "notes": 1000}
MAX_POLICY_BYTES = 4 * 1024 * 1024
_MAX_POLICIES = 50
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024


class PolicyIndexError(ValueError):
    """The index is wrong; the message says where."""


def blob_id(data: bytes) -> str:
    """Git's id for a file's bytes, to check a stored copy against the index."""
    return hashlib.sha1(
        b"blob %d\0" % len(data) + data, usedforsecurity=False
    ).hexdigest()


def parse(text: str, blobs: dict[str, tuple[str, int]]) -> list[dict[str, object]]:
    """Validate the whole index against the commit's files ({path: (blob id, size)}) or
    reject it. Each entry comes back with its ``blob`` and ``bytes`` filled in."""
    if len(text.encode()) > MAX_INDEX_BYTES:
        raise PolicyIndexError(f"{INDEX} is over 64 KB")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PolicyIndexError(f"{INDEX} isn't valid TOML: {exc}") from exc
    if set(data) - {"policy"}:
        extra = ", ".join(sorted(set(data) - {"policy"}))
        raise PolicyIndexError(f"{INDEX}: unknown top-level key {extra}")
    raw = data.get("policy", [])
    if not isinstance(raw, list) or len(raw) > _MAX_POLICIES:
        raise PolicyIndexError(
            f"{INDEX}: [[policy]] must be a list of at most {_MAX_POLICIES}"
        )
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    total = 0
    for i, item in enumerate(raw, 1):
        entry = check_entry(item, i)
        if entry["slug"] in seen:
            raise PolicyIndexError(
                f"{INDEX} policy {i}: {entry['slug']} is listed twice"
            )
        seen.add(str(entry["slug"]))
        found = blobs.get(str(entry["file"]))
        if found is None:
            raise PolicyIndexError(
                f"{INDEX} policy {i}: {entry['file']} isn't in the repo"
            )
        blob, size = found
        if size > MAX_POLICY_BYTES:
            raise PolicyIndexError(f"{INDEX} policy {i}: {entry['file']} is over 4 MB")
        total += size
        out.append({**entry, "blob": blob, "bytes": size})
    if total > _MAX_TOTAL_BYTES:
        raise PolicyIndexError(f"{INDEX}: the policies add up to over 64 MB")
    return out


def check_entry(item: object, i: int) -> dict[str, object]:
    """One entry's own fields, not whether its file exists; re-checks a saved one."""

    def bad(why: str) -> PolicyIndexError:
        return PolicyIndexError(f"{INDEX} policy {i}: {why}")

    if not isinstance(item, dict):
        raise bad("not a table")
    if set(item) - _KEYS - {"blob", "bytes"}:
        extra = set(item) - _KEYS - {"blob", "bytes"}
        raise bad(f"unknown key {', '.join(sorted(extra))}")
    if _REQUIRED - set(item):
        raise bad(f"missing {', '.join(sorted(_REQUIRED - set(item)))}")
    for key in _KEYS:
        if key in item and not isinstance(item[key], str):
            raise bad(f"{key} must be text")
    if not _SLUG_RE.fullmatch(item["slug"]):
        raise bad("slug must be 1-48 of a-z, 0-9 and -")
    if not _JOB_RE.fullmatch(item["job"]):
        raise bad("job must be the growbot_ job that trained it")
    if not _FILE_RE.fullmatch(item["file"]):
        raise bad("file must be policies/<folder>/<name>.json")
    if "run" in item and not _RUN_RE.fullmatch(item["run"]):
        raise bad("run must be a run id")
    for key, limit in _MAX_TEXT.items():
        if len(str(item.get(key, ""))) > limit:
            raise bad(f"{key} is over {limit} characters")
    if not " ".join(str(item["title"]).split()):
        raise bad("title is empty")
    entry = {k: item[k] for k in _KEYS if k in item}
    if "blob" in item:
        if not isinstance(item["blob"], str) or not _BLOB_RE.fullmatch(item["blob"]):
            raise bad("blob must be a git blob id")
        size = item.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise bad("bytes must be a size")
        entry |= {"blob": item["blob"], "bytes": size}
    return entry
