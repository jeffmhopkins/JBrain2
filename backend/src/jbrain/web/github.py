"""`web_fetch` for public GitHub repositories (docs/archive/GITHUB_FETCH_PLAN.md).

A GitHub page read as HTML is one file at a time, its link list capped, with no line
numbers and no way to search the repo. So a recognised GitHub URL is answered from a
repo **snapshot** instead: the repository's tarball, downloaded once and indexed in
memory, from which an overview, a whole-folder listing, a line-numbered file (or an
`#L10-L40` range) and a search across every text file are rendered as plain markdown for
`web_fetch` to window exactly like a page.

No API: unauthenticated `api.github.com` is 60 requests an hour per IP, shared with the
whole box, and a rate-limit 403 there would cost a day on the skip list. The archive comes
from codeload (not API-limited) and commits/releases from GitHub's public Atom feeds. No
token either — there is deliberately no GitHub credential on the box — so private repos
read as missing.

Why in memory and not the blob store: the archive is parsed straight from a bounded buffer
and only text files under the per-file cap are kept, so nothing is ever extracted to disk.
That leaves no raw path to guard and nothing to garbage-collect (blobs are content-addressed,
so deleting an evicted tarball's digest could take an owner attachment with it).

Every download goes through `WebFetcher.fetch_bytes`/`fetch_feed`, i.e. the per-hop SSRF
guard. A snapshot failure never reaches the 24h skip-list recorder: the caller falls back to
the plain page fetch, or says the repo is missing/private.
"""

from __future__ import annotations

import asyncio
import html
import io
import re
import sys
import tarfile
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

import regex as rx
import structlog

from jbrain.web.feeds import FeedItem, parse_feed
from jbrain.web.fetch import WebFetcher, WebFetchError

log = structlog.get_logger()

# Archive limits. Over any of them the snapshot is refused and the caller falls back to the
# plain page fetch: a monorepo should cost one refused download, not the box's memory.
MAX_COMPRESSED_BYTES = 100_000_000
MAX_UNCOMPRESSED_BYTES = 400_000_000
MAX_ENTRIES = 50_000
# What a snapshot keeps: text files up to the per-file cap, until the snapshot's text budget
# is spent. A file past either is still listed (with its size), just not readable.
MAX_FILE_BYTES = 1_000_000
MAX_SNAPSHOT_TEXT_BYTES = 64_000_000
# The process cache: long enough for a chat's follow-up reads (and a second chat soon
# after), bounded by count and by retained text.
CACHE_TTL_S = 1800.0
CACHE_MAX_SNAPSHOTS = 6
CACHE_MAX_TEXT_BYTES = 192_000_000
# A repo that 404'd, was over the caps, or was blocked is not re-downloaded on every retry.
NEGATIVE_TTL_S = 300.0
# One snapshot download (every archive URL tried, plus indexing) gets this long in all.
DOWNLOAD_DEADLINE_S = 120.0
# Snapshots built at once, across every repo: each one briefly holds its compressed archive
# plus the decoded text, so N agents reading N different repos must not stack N peaks.
CONCURRENT_SNAPSHOTS = 1
# Per-file bookkeeping beyond the text itself (the RepoFile, its path, the dict slot), so the
# cache budget counts what a snapshot really costs in memory rather than its raw bytes.
_FILE_OVERHEAD = 200
# A ref with slashes is ambiguous against the path after it; try at most this many splits.
MAX_REF_SEGMENTS = 4
# The repo page is read only for its description and default-branch name.
_REPO_PAGE_BYTES = 1_500_000

# Rendering caps.
_README_CHARS = 12_000
_TREE_TOP_ENTRIES = 80
_TREE_CHILD_DIRS = 12
_LANG_ROWS = 15
_FEED_COMMITS = 10
_FEED_RELEASES = 5
_SEARCH_MAX_LINES = 200
_SEARCH_MAX_PATHS = 50
_SEARCH_LINE_CHARS = 240
# A repo search runs a model-supplied pattern over attacker-written text: each line is cut to
# this many chars before matching, each match has its own timeout, and the whole search a
# time budget — past either it stops and says so.
_SEARCH_SCAN_CHARS = 2_000
_SEARCH_MATCH_TIMEOUT_S = 0.05
_SEARCH_BUDGET_S = 3.0
_LFS_HEAD = "version https://git-lfs.github.com/spec/v1"

_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_RAW_HOST = "raw.githubusercontent.com"
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_LINES_RE = re.compile(r"^L(\d+)(?:C\d+)?(?:-L(\d+)(?:C\d+)?)?$")
# First path segments on github.com that are site pages, not an owner.
_RESERVED_OWNERS = frozenset(
    {
        "about",
        "account",
        "apps",
        "codespaces",
        "collections",
        "contact",
        "copilot",
        "customer-stories",
        "dashboard",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "join",
        "login",
        "logout",
        "marketplace",
        "new",
        "notifications",
        "orgs",
        "organizations",
        "pricing",
        "pulls",
        "readme",
        "resources",
        "search",
        "security",
        "settings",
        "site",
        "solutions",
        "sponsors",
        "team",
        "topics",
        "trending",
        "users",
    }
)
_BINARY_EXTS = frozenset(
    {
        "7z", "a", "ai", "avi", "bin", "bmp", "bz2", "ckpt", "class", "db", "dll", "dylib",
        "eot", "exe", "flac", "gif", "gz", "h5", "ico", "jar", "jpeg", "jpg", "lib", "mkv",
        "mov", "mp3", "mp4", "npy", "npz", "o", "ogg", "onnx", "otf", "parquet", "pdf", "pkl",
        "png", "psd", "pt", "pyc", "pyo", "rar", "safetensors", "so", "sqlite", "tar", "tgz",
        "tif", "tiff", "ttf", "war", "wav", "webm", "webp", "whl", "woff", "woff2", "xz",
        "zip", "zst",
    }
)  # fmt: skip
# A blob's language, named as a code-fence tag, so jerv quoting the file fences it with the
# right one and the chat colours it. Only unambiguous extensions: a wrong tag colours worse
# than none, so anything not here gets no language line.
_FENCE_LANGS = {
    "py": "python", "pyi": "python", "ts": "typescript", "tsx": "tsx", "mts": "typescript",
    "js": "javascript", "jsx": "jsx", "mjs": "javascript", "cjs": "javascript",
    "sh": "bash", "bash": "bash", "zsh": "bash", "json": "json", "yaml": "yaml", "yml": "yaml",
    "sql": "sql", "rs": "rust", "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "cxx": "cpp",
    "hpp": "cpp", "go": "go", "html": "html", "htm": "html", "xml": "xml", "svg": "xml",
    "css": "css", "diff": "diff", "patch": "diff", "md": "markdown", "toml": "toml",
    "ini": "ini", "cfg": "ini", "java": "java", "kt": "kotlin", "rb": "ruby", "php": "php",
    "swift": "swift", "cs": "csharp", "lua": "lua", "r": "r", "scss": "scss",
}  # fmt: skip
_FENCE_NAMES = {"dockerfile": "dockerfile", "containerfile": "dockerfile", "makefile": "makefile"}


def _fence_lang(path: str) -> str | None:
    """The fence tag for a file, from its extension (or its whole name, for a Dockerfile)."""
    name = path.rsplit("/", 1)[-1].lower()
    if name in _FENCE_NAMES:
        return _FENCE_NAMES[name]
    if name.startswith("dockerfile."):
        return "dockerfile"
    if "." not in name.lstrip("."):
        return None
    return _FENCE_LANGS.get(name.rsplit(".", 1)[-1])


_DESC_RE = re.compile(r'<meta\s+name="description"\s+content="([^"]*)"', re.IGNORECASE)
_DEFAULT_BRANCH_RE = re.compile(r'"defaultBranch"\s*:\s*"([^"\\]{1,255})"')


# --- URL parsing ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubTarget:
    """A recognised GitHub URL. `rest` is everything after `tree/`/`blob/` (or after the repo
    on a raw URL): the ref AND the path, unsplit, because a ref may itself contain slashes —
    `ref_candidates` enumerates the possible splits. `lines` is a `#L10-L40` anchor."""

    owner: str
    repo: str
    kind: Literal["repo", "tree", "blob"]
    rest: tuple[str, ...] = ()
    lines: tuple[int, int] | None = None
    # A raw.githubusercontent.com URL: one file, answered from a snapshot only when one is
    # already cached — never worth downloading a whole repo for.
    raw: bool = False

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def ref_candidates(self) -> list[tuple[str | None, str]]:
        """(ref, path) splits to try, shortest ref first. None ref = the default branch.
        Shortest-first because git forbids branches `feature` and `feature/x` side by side,
        so the first split whose ref exists is almost always the intended one."""
        if self.kind == "repo" or not self.rest:
            return [(None, "")]
        out: list[tuple[str | None, str]] = []
        # A blob needs at least one path segment after its ref; a tree may be the ref alone.
        limit = len(self.rest) - 1 if self.kind == "blob" else len(self.rest)
        for i in range(1, min(limit, MAX_REF_SEGMENTS) + 1):
            ref = "/".join(self.rest[:i])
            out.append((None if ref == "HEAD" else ref, "/".join(self.rest[i:])))
        return out


def _clean_segments(path: str) -> list[str] | None:
    """URL path → decoded, non-empty segments; None when any segment could escape or smuggle
    (`..`, `.`, a NUL, a backslash) — such a URL is not one we answer from a snapshot."""
    segs = [unquote(s) for s in path.split("/") if s]
    for s in segs:
        # A decoded `/` (`%2f`) would let a ref carry its own `..` segments past this check
        # and steer the archive URL at another repository once the client collapses them.
        if s in {".", ".."} or "\x00" in s or "\\" in s or "/" in s:
            return None
    return segs


def _parse_lines(fragment: str) -> tuple[int, int] | None:
    m = _LINES_RE.match(fragment)
    if not m:
        return None
    start = max(1, int(m.group(1)))
    end = max(start, int(m.group(2))) if m.group(2) else start
    return start, end


def _valid_repo(owner: str, repo: str) -> bool:
    return (
        bool(_OWNER_RE.match(owner))
        and owner.lower() not in _RESERVED_OWNERS
        and bool(_REPO_RE.match(repo))
        and repo not in {".", ".."}
    )


def parse_github_url(url: str) -> GitHubTarget | None:
    """The GitHub target `url` names, or None for anything this reader does not answer (a
    user page, an issue, a PR, a non-GitHub host, a malformed path) — those keep the plain
    page fetch."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"}:
        return None
    host = (parts.hostname or "").lower()
    segs = _clean_segments(parts.path)
    if segs is None:
        return None
    lines = _parse_lines(parts.fragment)
    if host == _RAW_HOST:
        if len(segs) < 4 or not _valid_repo(segs[0], segs[1]):
            return None
        rest = segs[2:]
        # raw.githubusercontent.com/o/r/refs/heads/main/path — the fully qualified form.
        if len(rest) >= 4 and rest[0] == "refs" and rest[1] in {"heads", "tags"}:
            rest = rest[2:]
        return GitHubTarget(segs[0], segs[1], "blob", tuple(rest), lines, raw=True)
    if host not in _GITHUB_HOSTS or len(segs) < 2:
        return None
    owner, repo = segs[0], segs[1].removesuffix(".git")
    if not _valid_repo(owner, repo):
        return None
    if len(segs) == 2:
        return GitHubTarget(owner, repo, "repo")
    if segs[2] == "tree":
        if len(segs) == 3:
            return GitHubTarget(owner, repo, "repo")
        return GitHubTarget(owner, repo, "tree", tuple(segs[3:]))
    if segs[2] == "blob" and len(segs) >= 5:
        return GitHubTarget(owner, repo, "blob", tuple(segs[3:]), lines)
    return None


# --- The snapshot --------------------------------------------------------------------------


class SnapshotRefused(Exception):
    """The archive broke a limit (size, entry count). A clean refusal, not a crash."""


@dataclass(frozen=True)
class RepoFile:
    path: str
    size: int
    # None when not readable here; `note` says why (binary / too large / over budget).
    text: str | None
    note: str = ""


@dataclass
class RepoMeta:
    """Best-effort extras for the overview, fetched once per snapshot."""

    description: str = ""
    default_branch: str = ""
    commits: tuple[FeedItem, ...] = ()
    releases: tuple[FeedItem, ...] = ()
    feeds_ok: bool = False


@dataclass
class Snapshot:
    owner: str
    repo: str
    ref: str | None  # None = the default branch
    commit: str
    files: dict[str, RepoFile]
    skipped_links: int = 0
    skipped_unsafe: int = 0
    text_bytes: int = 0
    # What the snapshot costs in memory (decoded str sizes + per-file overhead) — the cache
    # budget's unit, since a str of non-ASCII text can take up to 4x its UTF-8 bytes.
    mem_bytes: int = 0
    meta: RepoMeta | None = None
    meta_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def label_ref(self) -> str:
        """The ref to show and to build follow-up URLs with."""
        if self.ref:
            return self.ref
        if self.meta and self.meta.default_branch:
            return self.meta.default_branch
        return "HEAD"


def _safe_relpath(name: str) -> str | None:
    """A tar member name → its repo-relative path (the archive's top directory stripped), or
    None when the name is absolute, climbs with `..`, carries a NUL, or is empty. Nothing is
    written to disk, but a canonical path is what every view and lookup keys on."""
    if "\x00" in name or name.startswith("/") or PurePosixPath(name).is_absolute():
        return None
    parts = [p for p in name.split("/") if p not in {"", "."}]
    if any(p == ".." for p in parts) or len(parts) < 2:
        return None
    return "/".join(parts[1:])


def _is_binary_name(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext in _BINARY_EXTS


def index_tarball(
    data: bytes,
    *,
    owner: str,
    repo: str,
    ref: str | None,
    max_uncompressed: int = MAX_UNCOMPRESSED_BYTES,
    max_entries: int = MAX_ENTRIES,
    max_file: int = MAX_FILE_BYTES,
    max_text: int = MAX_SNAPSHOT_TEXT_BYTES,
) -> Snapshot:
    """Index a gzip tarball (bytes) into a Snapshot, streaming member by member. Only regular
    files are ever read; every cap is checked from the header BEFORE the data is touched, so
    a decompression bomb costs at most `max_uncompressed` of inflate work. Raises
    SnapshotRefused past a cap, or for an archive that is not a readable tar.gz."""
    files: dict[str, RepoFile] = {}
    entries = declared = text_bytes = links = unsafe = 0
    commit = ""
    mem = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tf:
            for member in tf:
                entries += 1
                if entries > max_entries:
                    raise SnapshotRefused(f"more than {max_entries:,} files")
                if member.isdir():
                    continue
                # Every member's declared size counts, including ones skipped below: the
                # inflate work to skip past a member is the same as to read it.
                declared += member.size
                if declared > max_uncompressed:
                    raise SnapshotRefused(f"over {max_uncompressed // 1_000_000} MB uncompressed")
                if not member.isreg():
                    # Symlinks, hardlinks, devices, FIFOs: never followed, never read.
                    links += 1
                    continue
                rel = _safe_relpath(member.name)
                if rel is None:
                    unsafe += 1
                    continue
                if _is_binary_name(rel):
                    files[rel] = RepoFile(rel, member.size, None, "binary")
                    continue
                if member.size > max_file:
                    files[rel] = RepoFile(rel, member.size, None, "too large to index")
                    continue
                if text_bytes + member.size > max_text:
                    files[rel] = RepoFile(rel, member.size, None, "not indexed (repo too big)")
                    continue
                fh = tf.extractfile(member)
                raw = fh.read(member.size) if fh is not None else b""
                if b"\x00" in raw[:8192]:
                    files[rel] = RepoFile(rel, member.size, None, "binary")
                    continue
                text_bytes += len(raw)
                files[rel] = RepoFile(rel, member.size, raw.decode("utf-8", errors="replace"))
            commit = str(tf.pax_headers.get("comment", "")).strip()
        for f in files.values():
            mem += _FILE_OVERHEAD + sys.getsizeof(f.path)
            if f.text is not None:
                mem += sys.getsizeof(f.text)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise SnapshotRefused("the archive could not be read") from exc
    return Snapshot(
        owner=owner,
        repo=repo,
        ref=ref,
        commit=commit if re.fullmatch(r"[0-9a-f]{40}", commit) else "",
        files=files,
        skipped_links=links,
        skipped_unsafe=unsafe,
        text_bytes=text_bytes,
        mem_bytes=mem,
    )


# --- Views ---------------------------------------------------------------------------------


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _line_count(text: str) -> int:
    return len(_lines(text))


def _blob_url(snap: Snapshot, path: str) -> str:
    return f"https://github.com/{snap.slug}/blob/{snap.label_ref}/{path}"


def _tree_url(snap: Snapshot, path: str) -> str:
    base = f"https://github.com/{snap.slug}/tree/{snap.label_ref}"
    return f"{base}/{path}" if path else base


def _under(path: str, folder: str) -> bool:
    return not folder or path.startswith(folder + "/")


def _children(snap: Snapshot, folder: str) -> tuple[dict[str, list[RepoFile]], list[RepoFile]]:
    """A folder's direct children: sub-folders (each with every file beneath it) and files."""
    dirs: dict[str, list[RepoFile]] = {}
    files: list[RepoFile] = []
    prefix = folder + "/" if folder else ""
    for path, f in snap.files.items():
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix) :]
        if "/" in rest:
            dirs.setdefault(rest.split("/", 1)[0], []).append(f)
        else:
            files.append(f)
    return dirs, sorted(files, key=lambda f: f.path.lower())


def _ref_line(snap: Snapshot) -> str:
    if snap.ref:
        ref = f"ref `{snap.ref}`"
    elif snap.meta and snap.meta.default_branch:
        ref = f"default branch `{snap.meta.default_branch}`"
    else:
        ref = "default branch"
    return ref + (f" · commit {snap.commit[:12]}" if snap.commit else "")


def _snapshot_notes(snap: Snapshot) -> str:
    notes = []
    if snap.skipped_links:
        notes.append(f"{snap.skipped_links} symlink(s)/special file(s) not followed")
    if snap.skipped_unsafe:
        notes.append(f"{snap.skipped_unsafe} entr(ies) with unsafe paths dropped")
    return f"\n({'; '.join(notes)})" if notes else ""


def _languages(snap: Snapshot) -> str:
    stats: dict[str, list[int]] = {}
    for f in snap.files.values():
        if f.text is None:
            continue
        name = f.path.rsplit("/", 1)[-1]
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else name
        row = stats.setdefault(ext, [0, 0])
        row[0] += 1
        row[1] += _line_count(f.text)
    if not stats:
        return "_No text files._"
    ranked = sorted(stats.items(), key=lambda kv: (-kv[1][1], kv[0]))
    lines = ["| extension | files | lines |", "|---|---|---|"]
    lines += [f"| {ext} | {n} | {loc:,} |" for ext, (n, loc) in ranked[:_LANG_ROWS]]
    if len(ranked) > _LANG_ROWS:
        lines.append(f"| (+{len(ranked) - _LANG_ROWS} more) | | |")
    return "\n".join(lines)


def _tree_outline(snap: Snapshot) -> str:
    dirs, files = _children(snap, "")
    lines: list[str] = []
    for name in sorted(dirs, key=str.lower):
        below = dirs[name]
        lines.append(f"- {name}/ ({len(below)} files, {_fmt_size(sum(f.size for f in below))})")
        sub, _ = _children(snap, name)
        for i, child in enumerate(sorted(sub, key=str.lower)):
            if i == _TREE_CHILD_DIRS:
                lines.append(f"  - (+{len(sub) - _TREE_CHILD_DIRS} more folders)")
                break
            lines.append(f"  - {name}/{child}/ ({len(sub[child])} files)")
    lines += [f"- {f.path} ({_fmt_size(f.size)})" for f in files]
    if len(lines) > _TREE_TOP_ENTRIES:
        extra = len(lines) - _TREE_TOP_ENTRIES
        lines = lines[:_TREE_TOP_ENTRIES] + [f"- (+{extra} more entries — open a folder)"]
    return "\n".join(lines) or "_Empty repository._"


def _readme(snap: Snapshot, folder: str = "") -> RepoFile | None:
    prefix = folder + "/" if folder else ""
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md", "Readme.md"):
        f = snap.files.get(prefix + name)
        if f is not None and f.text is not None:
            return f
    return None


def _feed_lines(items: tuple[FeedItem, ...]) -> str:
    out = []
    for it in items:
        when = it.published.split("T", 1)[0] if it.published else ""
        title = " ".join(it.title.split())[:160]
        out.append(f"- {when + ' · ' if when else ''}{title} — {it.url}")
    return "\n".join(out)


def render_overview(snap: Snapshot) -> str:
    meta = snap.meta or RepoMeta()
    text_files = sum(1 for f in snap.files.values() if f.text is not None)
    total = sum(f.size for f in snap.files.values())
    parts = [f"# {snap.slug}\nhttps://github.com/{snap.slug}\n"]
    if meta.description:
        parts.append(meta.description + "\n")
    parts.append(
        f"Snapshot: {_ref_line(snap)} · {len(snap.files)} files ({text_files} readable"
        f" text) · {_fmt_size(total)}{_snapshot_notes(snap)}\n"
    )
    parts.append("## Languages (text files, by extension)\n" + _languages(snap) + "\n")
    parts.append("## File tree\n" + _tree_outline(snap) + "\n")
    readme = _readme(snap)
    if readme is not None and readme.text is not None:
        body = readme.text[:_README_CHARS]
        if len(readme.text) > _README_CHARS:
            body += f"\n\n[README truncated — read it whole at {_blob_url(snap, readme.path)}]"
        parts.append(f"## {readme.path}\n{body}\n")
    if meta.feeds_ok or meta.commits or meta.releases:
        if meta.commits:
            parts.append("## Recent commits\n" + _feed_lines(meta.commits) + "\n")
        if meta.releases:
            parts.append("## Releases\n" + _feed_lines(meta.releases) + "\n")
    parts.append(
        "## Reading more\n"
        f"- A folder: web_fetch {_tree_url(snap, '<folder>')}\n"
        f"- A file: web_fetch {_blob_url(snap, '<path>')} (add #L10-L40 for a line range)\n"
        '- Search every file: web_fetch this URL with find="<term>" (regex=true for a pattern)'
    )
    return "\n".join(parts)


def render_tree(snap: Snapshot, folder: str) -> str:
    dirs, files = _children(snap, folder)
    shown = folder or "(repository root)"
    head = f"# {snap.slug}/{folder}".rstrip("/") + f"\n{_tree_url(snap, folder)}\n\n"
    total = sum(f.size for f in snap.files.values() if _under(f.path, folder))
    lines = [
        f"Folder `{shown}` at {_ref_line(snap)} — {len(dirs)} folder(s), {len(files)} file(s),"
        f" {_fmt_size(total)} in all."
    ]
    for name in sorted(dirs, key=str.lower):
        below = dirs[name]
        lines.append(f"- {name}/ — {len(below)} files, {_fmt_size(sum(f.size for f in below))}")
    for f in files:
        name = f.path.rsplit("/", 1)[-1]
        detail = f"{_line_count(f.text):,} lines" if f.text is not None else f.note
        lines.append(f"- {name} — {_fmt_size(f.size)}, {detail}")
    readme = _readme(snap, folder)
    if readme is not None and readme.text is not None and folder:
        lines.append(f"\nThis folder has a README: web_fetch {_blob_url(snap, readme.path)}")
    child = (folder + "/" if folder else "") + "<name>"
    lines.append(
        f"\nOpen a file with web_fetch {_blob_url(snap, child)}"
        f" or a sub-folder with {_tree_url(snap, child)};"
        ' find="<term>" on this URL searches every file beneath it.'
    )
    return head + "\n".join(lines)


def _lines(text: str) -> list[str]:
    """A file's lines as GitHub numbers them: split on "\n" only (`splitlines` would also break
    on \f, \v, U+2028 and a lone \r and shift every number after it), one trailing "\r"
    dropped from each, no phantom empty line after a final newline."""
    out = [line.removesuffix("\r") for line in text.split("\n")]
    if out and out[-1] == "":
        out.pop()
    return out


def _lfs_note(text: str) -> str | None:
    """A Git LFS pointer stands in for a file stored outside the repo: say so in one line."""
    if not text.startswith(_LFS_HEAD):
        return None
    size = next((ln.split(" ", 1)[1] for ln in _lines(text) if ln.startswith("size ")), "")
    real = f" ({_fmt_size(int(size))})" if size.isdigit() else ""
    return (
        f"This file is a Git LFS pointer: the real content{real} is stored outside the"
        " repository archive and is not readable here."
    )


def render_blob(snap: Snapshot, f: RepoFile, lines: tuple[int, int] | None) -> str:
    """The file (whose text the caller has checked is present) with line numbers."""
    text = f.text or ""
    head = f"# {f.path} — {snap.slug}\n{_blob_url(snap, f.path)}\n\n"
    lfs = _lfs_note(text)
    if lfs is not None:
        return head + lfs
    all_lines = _lines(text)
    n = len(all_lines)
    start, end = 1, n
    span = ""
    if lines is not None:
        if lines[0] > n:
            return head + (
                f"`{f.path}` has {n:,} line(s), so there is no line {lines[0]}. Read it without"
                f" the #L anchor, or pick a range within 1–{n}."
            )
        start, end = lines[0], min(lines[1], n)
        span = f" · showing lines {start}–{end}"
    width = len(str(max(end, 1)))
    body = "\n".join(f"{i:>{width}}  {all_lines[i - 1]}" for i in range(start, end + 1))
    lang = _fence_lang(f.path)
    tag = f" · language: {lang}" if lang else ""
    return (
        head
        + f"`{f.path}` at {_ref_line(snap)} — {n:,} lines, {_fmt_size(f.size)}{tag}{span}\n\n"
        + body
    )


class _SearchStopped(Exception):
    """The search's time budget ran out, or one match exceeded its own timeout."""


def render_search(
    snap: Snapshot,
    folder: str,
    term: str,
    *,
    regex: bool,
    budget_s: float = _SEARCH_BUDGET_S,
    match_timeout_s: float = _SEARCH_MATCH_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Every line matching `term` in the text files under `folder` — path, line number, the
    line — plus files whose PATH matches. Bounded in output AND in work: the pattern is the
    model's and the text is a stranger's, so each line is cut to `_SEARCH_SCAN_CHARS`, each
    match has a timeout (the `regex` engine can be stopped mid-backtrack; `re` cannot), and the
    whole search a budget. A stopped search reports what it found and that it stopped."""
    label = f"regex '{term}'" if regex else f"'{term}'"
    scope = f"{snap.slug}/{folder}" if folder else snap.slug
    head = f"# Search {label} in {scope}\n{_tree_url(snap, folder)}\n\n"
    try:
        pattern = rx.compile(term if regex else rx.escape(term), rx.IGNORECASE | rx.VERSION0)
    except rx.error as exc:
        return head + f"Invalid regex for find: {exc}. Fix the pattern, or drop regex=true."
    deadline = clock() + budget_s

    def hit(text: str) -> bool:
        if clock() > deadline:
            raise _SearchStopped
        try:
            return pattern.search(text[:_SEARCH_SCAN_CHARS], timeout=match_timeout_s) is not None
        except TimeoutError as exc:
            raise _SearchStopped from exc

    hits: list[str] = []
    path_hits: list[str] = []
    match_total = file_total = path_total = searched = 0
    stopped = False
    try:
        for path in sorted(snap.files, key=str.lower):
            if not _under(path, folder):
                continue
            f = snap.files[path]
            if hit(path):
                path_total += 1
                if len(path_hits) < _SEARCH_MAX_PATHS:
                    path_hits.append(f"- {path}")
            if f.text is None:
                continue
            searched += 1
            file_hits = []
            try:
                for no, line in enumerate(_lines(f.text), 1):
                    if hit(line):
                        match_total += 1
                        if len(hits) + len(file_hits) < _SEARCH_MAX_LINES:
                            file_hits.append(f"  {no}: {line.strip()[:_SEARCH_LINE_CHARS]}")
            finally:
                if file_hits:
                    file_total += 1
                    hits.append(path)
                    hits.extend(file_hits)
    except _SearchStopped:
        stopped = True
    stop_note = (
        "\n[Search stopped early: the pattern was too slow over this repo (a per-match timeout"
        " or the search's time budget). Results above are partial — use a simpler or literal"
        " term, or search a sub-folder's tree URL.]"
        if stopped
        else ""
    )
    if not match_total and not path_total:
        return (
            head
            + (
                f"No match for {label} in {searched} text file(s) at {_ref_line(snap)}. Try a"
                " different term, or open the folder listing to browse."
            )
            + stop_note
        )
    shown = sum(1 for h in hits if h.startswith("  "))
    out = [
        f"[{match_total} matching line(s) in {file_total} file(s); searched {searched} text"
        f" file(s) at {_ref_line(snap)}; showing {shown}. Open a hit with web_fetch"
        f" {_blob_url(snap, '<path>')}#L<line>.]"
    ]
    if path_hits:
        out.append(f"\nPaths matching ({path_total}):\n" + "\n".join(path_hits))
    if hits:
        out.append("\nLines matching:\n" + "\n".join(hits))
    if match_total > shown:
        out.append(
            f"\n[+{match_total - shown} more matching line(s) not shown — narrow the term or"
            " search a sub-folder's tree URL.]"
        )
    return head + "\n".join(out) + stop_note


# --- The reader ----------------------------------------------------------------------------


class GitHubUnavailable(Exception):
    """The snapshot could not answer. `fallback` True → the caller reads the URL the ordinary
    way (with `message` as a note) — or, when `plain_url` is set, reads THAT URL instead (a
    file the snapshot lists but cannot show, read from raw.githubusercontent.com); False →
    `message` IS the reply (the branch/file's repo is missing or private, so a plain fetch
    would only 404 again)."""

    def __init__(self, message: str, *, fallback: bool, plain_url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.fallback = fallback
        self.plain_url = plain_url


class _NotFound(Exception):
    """This ref (or the whole repo) does not exist publicly."""


@dataclass(frozen=True)
class GitHubPage:
    """A rendered view for web_fetch to window. `find` is the term the caller should still
    apply as a page find ("" when the view already consumed it — a repo/tree search)."""

    title: str
    url: str
    text: str
    find: str


_Key = tuple[str, str, str | None]
# A remembered failure: which kind, and its message. The exception is rebuilt per hit rather
# than one instance re-raised to every caller (a shared instance accumulates tracebacks).
_Miss = tuple[Literal["not_found", "refused", "unavailable"], str]


def _ref_path(ref: str) -> str:
    """A ref as URL path segments, each percent-encoded on its own. Parsing already refused
    `.`/`..`/encoded-slash segments; encoding is the second lock on the URL we build."""
    return "/".join(quote(part, safe="") for part in ref.split("/"))


class GitHubReader:
    """Answers GitHub URLs from cached repo snapshots, downloading each through `fetcher`
    (the SSRF-guarded path) at most once at a time per repo@ref, and building at most
    `CONCURRENT_SNAPSHOTS` at once overall. One reader serves the whole process (main.py
    builds one), so the instance-level gate is the process-wide one; it is not a module
    global only because an asyncio primitive must not outlive the event loop it was used on."""

    def __init__(
        self,
        fetcher: WebFetcher,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = CACHE_TTL_S,
        max_snapshots: int = CACHE_MAX_SNAPSHOTS,
        max_text_bytes: int = CACHE_MAX_TEXT_BYTES,
        max_compressed: int = MAX_COMPRESSED_BYTES,
        deadline_s: float = DOWNLOAD_DEADLINE_S,
        concurrent: int = CONCURRENT_SNAPSHOTS,
        index: Callable[..., Snapshot] = index_tarball,
    ) -> None:
        self._fetcher = fetcher
        self._clock = clock
        self._ttl = ttl_s
        self._max_snapshots = max_snapshots
        self._max_mem = max_text_bytes
        self._max_compressed = max_compressed
        self._deadline = deadline_s
        self._gate = asyncio.Semaphore(concurrent)
        self._index = index
        self._cache: OrderedDict[_Key, tuple[Snapshot, float]] = OrderedDict()
        self._negative: dict[_Key, tuple[_Miss, float]] = {}
        self._inflight: dict[_Key, asyncio.Task[Snapshot]] = {}

    @staticmethod
    def handles(url: str) -> bool:
        return parse_github_url(url) is not None

    # -- cache --

    def _key(self, owner: str, repo: str, ref: str | None) -> _Key:
        return (owner.lower(), repo.lower(), ref)

    def _cached(self, key: _Key) -> Snapshot | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        snap, expires = hit
        if self._clock() >= expires:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return snap

    def _store(self, key: _Key, snap: Snapshot) -> None:
        self._cache[key] = (snap, self._clock() + self._ttl)
        self._cache.move_to_end(key)
        self._evict()

    def _evict(self) -> None:
        def unique() -> list[Snapshot]:
            # A snapshot aliased under two keys counts once.
            return list({id(s): s for s, _ in self._cache.values()}.values())

        while self._cache and (
            len(unique()) > self._max_snapshots
            or sum(s.mem_bytes for s in unique()) > self._max_mem
        ):
            self._cache.popitem(last=False)

    def _remember_miss(self, key: _Key, miss: _Miss) -> None:
        now = self._clock()
        for k in [k for k, (_m, expires) in self._negative.items() if now >= expires]:
            del self._negative[k]
        self._negative[key] = (miss, now + NEGATIVE_TTL_S)

    def _negative_hit(self, key: _Key) -> Exception | None:
        hit = self._negative.get(key)
        if hit is None:
            return None
        (kind, message), expires = hit
        if self._clock() >= expires:
            del self._negative[key]
            return None
        if kind == "not_found":
            return _NotFound()
        if kind == "refused":
            return SnapshotRefused(message)
        return GitHubUnavailable(message, fallback=True)

    # -- download --

    def _archive_urls(self, owner: str, repo: str, ref: str | None) -> list[str]:
        if ref is not None:
            return [f"https://codeload.github.com/{owner}/{repo}/tar.gz/{_ref_path(ref)}"]
        # The default branch without the API: HEAD through github.com's archive route (which
        # redirects to codeload), codeload's own HEAD, then the two conventional names.
        return [
            f"https://github.com/{owner}/{repo}/archive/HEAD.tar.gz",
            f"https://codeload.github.com/{owner}/{repo}/tar.gz/HEAD",
            f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/main",
            f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/master",
        ]

    async def _download(self, owner: str, repo: str, ref: str | None) -> Snapshot:
        for url in self._archive_urls(owner, repo, ref):
            try:
                _ctype, body = await self._fetcher.fetch_bytes(
                    url, max_bytes=self._max_compressed + 1
                )
            except WebFetchError as exc:
                if exc.status == 404:
                    continue
                log.info("github.archive_failed", url=url, status=exc.status, error=str(exc))
                raise GitHubUnavailable(
                    f"the repo archive could not be downloaded ({exc})", fallback=True
                ) from exc
            if len(body) > self._max_compressed:
                raise SnapshotRefused(
                    f"the archive is over {self._max_compressed // 1_000_000} MB compressed"
                )
            snap = await asyncio.to_thread(self._index, body, owner=owner, repo=repo, ref=ref)
            log.info("github.snapshot", repo=f"{owner}/{repo}", ref=ref, files=len(snap.files))
            return snap
        raise _NotFound

    async def _snapshot(self, owner: str, repo: str, ref: str | None) -> Snapshot:
        key = self._key(owner, repo, ref)
        snap = self._cached(key)
        if snap is not None:
            return snap
        neg = self._negative_hit(key)
        if neg is not None:
            raise neg
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fill(key, owner, repo, ref))
            # Retrieve the outcome even when every waiter was cancelled, so a failed download
            # nobody is left awaiting is not reported as a never-retrieved exception.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._inflight[key] = task
        # Shielded: a caller cancelled mid-download must not cancel the download every other
        # waiter (and the cache) is relying on.
        return await asyncio.shield(task)

    async def _fill(self, key: _Key, owner: str, repo: str, ref: str | None) -> Snapshot:
        """One download for every concurrent caller of `key`; its outcome — a snapshot or a
        miss of any kind — is cached here, not by whichever caller happened to start it."""
        try:
            async with self._gate:
                snap = await asyncio.wait_for(
                    self._download(owner, repo, ref), timeout=self._deadline
                )
        except _NotFound:
            self._remember_miss(key, ("not_found", ""))
            raise
        except SnapshotRefused as exc:
            self._remember_miss(key, ("refused", str(exc)))
            raise
        except GitHubUnavailable as exc:
            self._remember_miss(key, ("unavailable", exc.message))
            raise
        except TimeoutError as exc:
            message = f"the repo archive took over {self._deadline:.0f} s to download"
            self._remember_miss(key, ("unavailable", message))
            raise GitHubUnavailable(message, fallback=True) from exc
        finally:
            self._inflight.pop(key, None)
        self._store(key, snap)
        return snap

    async def _resolve(self, target: GitHubTarget) -> tuple[Snapshot, str]:
        """The snapshot + in-repo path for `target`, trying each ref split in turn."""
        candidates = target.ref_candidates()
        for ref, path in candidates:
            snap = self._cached(self._key(target.owner, target.repo, ref))
            if snap is not None:
                return snap, path
        for ref, path in candidates:
            try:
                return await self._snapshot(target.owner, target.repo, ref), path
            except _NotFound:
                continue
            except SnapshotRefused as exc:
                raise GitHubUnavailable(
                    f"{target.slug} is too large to snapshot ({exc})", fallback=True
                ) from exc
        if target.kind == "repo":
            # Two path segments that are not a public repo may still be a real page (a site
            # section this parser does not know): read it the ordinary way.
            raise GitHubUnavailable(
                f"{target.slug} has no public repository archive", fallback=True
            )
        raise GitHubUnavailable(
            f"GitHub repository {target.slug} (or the branch/tag named in the URL) was not"
            " found — it does not exist, or it is private (only public repos can be read)."
            " Check the spelling, or web_search for the right repository.",
            fallback=False,
        )

    def _cached_only(self, target: GitHubTarget) -> tuple[Snapshot, str] | None:
        for ref, path in target.ref_candidates():
            snap = self._cached(self._key(target.owner, target.repo, ref))
            if snap is not None:
                return snap, path
        return None

    async def _ensure_meta(self, snap: Snapshot) -> None:
        """Fetch the overview extras once per snapshot: the description and default-branch
        name off the repo page, and the commit/release Atom feeds. All best-effort."""
        async with snap.meta_lock:
            if snap.meta is not None:
                return
            meta = RepoMeta()
            base = f"https://github.com/{snap.owner}/{snap.repo}"
            commits_ref = _ref_path(snap.commit or snap.ref or "HEAD")
            page, commits, releases = await asyncio.gather(
                self._fetcher.fetch_bytes(base, max_bytes=_REPO_PAGE_BYTES),
                self._fetcher.fetch_feed(f"{base}/commits/{commits_ref}.atom"),
                self._fetcher.fetch_feed(f"{base}/releases.atom"),
                return_exceptions=True,
            )
            if isinstance(page, tuple):
                raw = page[1].decode("utf-8", errors="replace")
                m = _DESC_RE.search(raw)
                if m:
                    meta.description = _clean_description(html.unescape(m.group(1)), snap)
                b = _DEFAULT_BRANCH_RE.search(raw)
                if b:
                    meta.default_branch = b.group(1)
            if isinstance(commits, bytes):
                meta.commits = tuple(parse_feed(commits)[:_FEED_COMMITS])
                meta.feeds_ok = True
            if isinstance(releases, bytes):
                meta.releases = tuple(parse_feed(releases)[:_FEED_RELEASES])
            snap.meta = meta
            if snap.ref is None and meta.default_branch:
                # A follow-up `tree/main/...` link built from the overview hits this snapshot.
                key = self._key(snap.owner, snap.repo, meta.default_branch)
                if self._cached(key) is None:
                    self._store(key, snap)

    async def read(self, url: str, *, find: str = "", regex: bool = False) -> GitHubPage | None:
        """The rendered view for `url`, or None when the ordinary page fetch should answer it
        (not a GitHub URL this reader answers, or a raw file with no snapshot already cached).
        Raises GitHubUnavailable when the snapshot cannot answer (see its `fallback`)."""
        target = parse_github_url(url)
        if target is None:
            return None
        if target.raw:
            # One file: a plain fetch reads it whole (PDFs and 5 MB windows included), so a
            # repo download is worth it only when a snapshot is already here and has the text.
            cached = self._cached_only(target)
            if cached is None:
                return None
            snap, path = cached
            f = snap.files.get(path)
            if f is None or f.text is None:
                return None
            return self._file_page(snap, f, target, find)
        snap, path = await self._resolve(target)
        if target.kind == "repo" or (target.kind == "tree" and not path):
            if find:
                return await self._search(snap, "", find, regex)
            await self._ensure_meta(snap)
            return GitHubPage(
                snap.slug, f"https://github.com/{snap.slug}", render_overview(snap), ""
            )
        f = snap.files.get(path)
        if f is not None:
            if f.text is None:
                # Listed but not shown here (binary, over the per-file cap, over the budget):
                # the raw file through the ordinary fetch reads PDFs and big text in windows.
                raw_url = (
                    f"https://raw.githubusercontent.com/{snap.owner}/{snap.repo}/"
                    f"{_ref_path(snap.commit or snap.label_ref)}/{_ref_path(f.path)}"
                )
                raise GitHubUnavailable(
                    f"`{f.path}` is {_fmt_size(f.size)} and {f.note} in the snapshot, so it is"
                    f" read from {raw_url}",
                    fallback=True,
                    plain_url=raw_url,
                )
            # A tree URL that names a file (or a blob URL) reads the file.
            return self._file_page(snap, f, target, find)
        if any(p.startswith(path + "/") for p in snap.files):
            if find:
                return await self._search(snap, path, find, regex)
            return GitHubPage(
                f"{snap.slug}/{path}", _tree_url(snap, path), render_tree(snap, path), ""
            )
        # A path that is not there: say so, with the nearest folder that is.
        parent = path
        while parent and not any(p.startswith(parent + "/") for p in snap.files):
            parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
        return GitHubPage(
            f"{snap.slug}: {path} not found",
            _tree_url(snap, path),
            f"`{path}` does not exist in {snap.slug} at {_ref_line(snap)}. The nearest"
            f" folder:\n\n{render_tree(snap, parent)}",
            "",
        )

    @staticmethod
    def _file_page(snap: Snapshot, f: RepoFile, target: GitHubTarget, find: str) -> GitHubPage:
        return GitHubPage(
            f"{f.path} — {snap.slug}",
            _blob_url(snap, f.path),
            render_blob(snap, f, target.lines),
            find,
        )

    async def _search(self, snap: Snapshot, folder: str, term: str, regex: bool) -> GitHubPage:
        # Off the event loop: a search over a full snapshot is real CPU even when bounded.
        text = await asyncio.to_thread(render_search, snap, folder, term, regex=regex)
        return GitHubPage(f"Search '{term}' — {snap.slug}", _tree_url(snap, folder), text, "")


def _clean_description(desc: str, snap: Snapshot) -> str:
    """GitHub's meta description appends boilerplate; keep the repo's own sentence."""
    desc = " ".join(desc.split())
    boiler = f"Contribute to {snap.owner}/{snap.repo} development by creating an account on GitHub."
    desc = desc.replace(boiler, "").strip()
    if desc.lower().endswith(f"- {snap.slug.lower()}"):
        desc = desc[: -len(snap.slug) - 2].strip()
    return desc.rstrip(" -")
