"""web_fetch reads public GitHub repos from a snapshot (docs/archive/GITHUB_FETCH_PLAN.md).

URL parsing, safe tar indexing, every view, the cache and single-flight, and the web_fetch
seams (identical presentation, fallbacks, no skip-list entry). No network: the real
WebFetcher runs over an httpx MockTransport serving fake tarballs and Atom feeds."""

from __future__ import annotations

import asyncio
import io
import socket
import tarfile
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.webtools import _present_result, build_web_handlers
from jbrain.db.session import SessionContext
from jbrain.web import fetch as fetch_mod
from jbrain.web import github as gh
from jbrain.web.domain_health import DomainSkipRepo
from jbrain.web.fetch import WebFetcher, window_text
from jbrain.web.github import (
    GitHubReader,
    GitHubTarget,
    GitHubUnavailable,
    SnapshotRefused,
    index_tarball,
    parse_github_url,
)
from jbrain.web.search import SearxngClient

SHA = "0123456789abcdef0123456789abcdef01234567"


def _ctx() -> ToolContext:
    return ToolContext(session=SessionContext(principal_kind="owner"), scopes=())


# --- fixtures ------------------------------------------------------------------------------


def _tarball(
    files: dict[str, bytes],
    *,
    top: str = "repo-main",
    comment: str = SHA,
    extra: Callable[[tarfile.TarFile], None] | None = None,
) -> bytes:
    """A GitHub-shaped tar.gz: a pax global header carrying the commit, one top directory."""
    buf = io.BytesIO()
    with tarfile.open(
        fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": comment}
    ) as tf:
        d = tarfile.TarInfo(top)
        d.type = tarfile.DIRTYPE
        tf.addfile(d)
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if extra is not None:
            extra(tf)
    return buf.getvalue()


_REPO_FILES = {
    "README.md": b"# Demo\n\nA demo repo about widgets.\n",
    "setup.py": b"from setuptools import setup\nsetup(name='demo')\n",
    "src/demo/__init__.py": b"VERSION = '1.0'\n",
    "src/demo/core.py": b"def widget():\n    return 'widget'\n\n\ndef gadget():\n    pass\n",
    "src/demo/util/helpers.py": b"# helpers\nWIDGET_COUNT = 3\n",
    "docs/guide.md": b"# Guide\n\nUse the widget.\n",
    "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary",
}

_ATOM_COMMITS = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Recent Commits to demo:main</title>
<entry><title>Fix the widget</title><link href="https://github.com/acme/demo/commit/abc"/>
<updated>2026-10-01T12:00:00Z</updated><id>1</id></entry>
</feed>"""
_ATOM_RELEASES = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Release notes from demo</title>
<entry><title>v1.0</title><link href="https://github.com/acme/demo/releases/tag/v1.0"/>
<updated>2026-09-01T12:00:00Z</updated><id>2</id></entry>
</feed>"""
_REPO_HTML = (
    b'<html><head><meta name="description" content="Widgets for everyone. Contribute to'
    b' acme/demo development by creating an account on GitHub.">'
    b'</head><body><script>{"defaultBranch":"main"}</script></body></html>'
)


class _GitHub:
    """A fake github.com/codeload. `archives` maps a request URL to tarball bytes; anything
    else 404s. Every request is logged."""

    def __init__(self, archives: dict[str, bytes] | None = None) -> None:
        tb = _tarball(_REPO_FILES)
        self.archives = (
            archives
            if archives is not None
            else {"https://github.com/acme/demo/archive/HEAD.tar.gz": tb}
        )
        self.calls: list[str] = []
        self.status: dict[str, int] = {}
        self.gate: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if self.gate is not None and url in self.archives:
            await self.gate.wait()
        if url in self.status:
            return httpx.Response(self.status[url])
        if url in self.archives:
            return httpx.Response(
                200, content=self.archives[url], headers={"content-type": "application/x-gzip"}
            )
        if url.endswith("/releases.atom"):
            return httpx.Response(200, content=_ATOM_RELEASES)
        if "/commits/" in url and url.endswith(".atom"):
            return httpx.Response(200, content=_ATOM_COMMITS)
        if url == "https://github.com/acme/demo":
            return httpx.Response(200, content=_REPO_HTML, headers={"content-type": "text/html"})
        return httpx.Response(404)

    def archive_calls(self) -> list[str]:
        return [c for c in self.calls if c in self.archives or "tar.gz" in c]


def _reader(fake: _GitHub, **kw: Any) -> GitHubReader:
    return GitHubReader(WebFetcher(transport=httpx.MockTransport(fake)), **kw)


# --- URL parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/demo", GitHubTarget("acme", "demo", "repo")),
        ("https://www.github.com/acme/demo/", GitHubTarget("acme", "demo", "repo")),
        ("https://github.com/acme/demo.git", GitHubTarget("acme", "demo", "repo")),
        ("https://github.com/acme/demo?tab=readme", GitHubTarget("acme", "demo", "repo")),
        ("https://github.com/acme/demo/tree", GitHubTarget("acme", "demo", "repo")),
        (
            "https://github.com/acme/demo/tree/main/src",
            GitHubTarget("acme", "demo", "tree", ("main", "src")),
        ),
        (
            "https://github.com/acme/demo/blob/main/src/a.py#L10-L40",
            GitHubTarget("acme", "demo", "blob", ("main", "src", "a.py"), (10, 40)),
        ),
        (
            "https://github.com/acme/demo/blob/main/a.py#L7",
            GitHubTarget("acme", "demo", "blob", ("main", "a.py"), (7, 7)),
        ),
        (
            "https://github.com/acme/demo/blob/main/a.py#L9C2-L3C1",
            GitHubTarget("acme", "demo", "blob", ("main", "a.py"), (9, 9)),
        ),
        (
            "https://github.com/acme/demo/blob/main/a.py#readme",
            GitHubTarget("acme", "demo", "blob", ("main", "a.py")),
        ),
        (
            "https://raw.githubusercontent.com/acme/demo/main/src/a.py",
            GitHubTarget("acme", "demo", "blob", ("main", "src", "a.py"), raw=True),
        ),
        (
            "https://raw.githubusercontent.com/acme/demo/refs/heads/dev/a.py",
            GitHubTarget("acme", "demo", "blob", ("dev", "a.py"), raw=True),
        ),
        (
            "https://github.com/acme/demo/blob/main/my%20file.txt",
            GitHubTarget("acme", "demo", "blob", ("main", "my file.txt")),
        ),
    ],
)
def test_recognised_shapes(url: str, expected: GitHubTarget) -> None:
    assert parse_github_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme",  # a user page
        "https://github.com/acme/demo/issues/4",
        "https://github.com/acme/demo/pull/9",
        "https://github.com/acme/demo/blob/main",  # a blob needs a path
        "https://github.com/orgs/acme",
        "https://github.com/settings/profile",
        "https://github.com/acme/demo/blob/main/../../etc/passwd",
        "https://github.com/acme/demo/blob/main/%2e%2e/x",
        "https://github.com/acme/demo/blob/main/a%00b",
        "https://github.com/acme/demo/blob/main/a%5Cb",
        "https://github.com/-bad/demo",
        "https://github.com/acme/..",
        "https://raw.githubusercontent.com/acme/demo/main",  # no path
        "https://gitlab.com/acme/demo",
        "ftp://github.com/acme/demo",
        "http://[::1",  # unparseable
        "",
    ],
)
def test_unhandled_urls_keep_the_plain_fetch(url: str) -> None:
    assert parse_github_url(url) is None
    assert not GitHubReader.handles(url)


def test_a_slashed_ref_is_tried_shortest_first() -> None:
    tree = GitHubTarget("a", "b", "tree", ("feature", "x", "src"))
    assert tree.ref_candidates() == [
        ("feature", "x/src"),
        ("feature/x", "src"),
        ("feature/x/src", ""),
    ]
    blob = GitHubTarget("a", "b", "blob", ("feature", "x", "f.py"))
    assert blob.ref_candidates() == [("feature", "x/f.py"), ("feature/x", "f.py")]
    assert GitHubTarget("a", "b", "tree", ("HEAD",)).ref_candidates() == [(None, "")]
    deep = GitHubTarget("a", "b", "tree", tuple("abcdefg"))
    assert len(deep.ref_candidates()) == gh.MAX_REF_SEGMENTS
    assert GitHubTarget("a", "b", "repo").ref_candidates() == [(None, "")]


# --- tar safety ----------------------------------------------------------------------------


def _special_members(tf: tarfile.TarFile) -> None:
    for name, kind in (
        ("repo-main/link", tarfile.SYMTYPE),
        ("repo-main/hard", tarfile.LNKTYPE),
        ("repo-main/dev", tarfile.CHRTYPE),
        ("repo-main/fifo", tarfile.FIFOTYPE),
    ):
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    for name in ("../evil.txt", "/abs/evil.txt", "repo-main/a/../../evil.txt", "toplevel-only"):
        info = tarfile.TarInfo(name)
        data = b"evil"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


def test_links_devices_and_unsafe_paths_are_never_read() -> None:
    snap = index_tarball(
        _tarball({"ok.txt": b"fine\n"}, extra=_special_members), owner="a", repo="b", ref=None
    )
    assert set(snap.files) == {"ok.txt"}
    assert snap.skipped_links == 4
    assert snap.skipped_unsafe == 4
    assert snap.commit == SHA


def test_safe_relpath_rules() -> None:
    assert gh._safe_relpath("top/a/b.py") == "a/b.py"
    assert gh._safe_relpath("top/./a//b.py") == "a/b.py"
    for bad in ("/top/a", "top/../a", "top/a\x00b", "top", ""):
        assert gh._safe_relpath(bad) is None


def test_binaries_are_listed_not_decoded() -> None:
    snap = index_tarball(
        _tarball({"img.PNG": b"text-ish", "blob.dat": b"ab\x00cd", "a.txt": b"hi"}),
        owner="a",
        repo="b",
        ref=None,
    )
    assert snap.files["img.PNG"].text is None and snap.files["img.PNG"].note == "binary"
    assert snap.files["blob.dat"].text is None and snap.files["blob.dat"].note == "binary"
    assert snap.files["a.txt"].text == "hi"
    assert snap.text_bytes == 2


def test_a_huge_file_is_listed_with_its_size_only() -> None:
    snap = index_tarball(
        _tarball({"big.txt": b"x" * 50, "small.txt": b"y"}),
        owner="a",
        repo="b",
        ref=None,
        max_file=10,
    )
    assert snap.files["big.txt"].text is None
    assert snap.files["big.txt"].note == "too large to index"
    assert snap.files["big.txt"].size == 50
    assert snap.files["small.txt"].text == "y"


def test_the_text_budget_stops_retaining() -> None:
    snap = index_tarball(
        _tarball({"a.txt": b"aaaa", "b.txt": b"bbbb"}), owner="a", repo="b", ref=None, max_text=6
    )
    assert snap.files["a.txt"].text == "aaaa"
    assert snap.files["b.txt"].text is None
    assert "not indexed" in snap.files["b.txt"].note


def test_too_many_files_is_refused() -> None:
    files = {f"f{i}.txt": b"x" for i in range(10)}
    with pytest.raises(SnapshotRefused, match="more than 5 files"):
        index_tarball(_tarball(files), owner="a", repo="b", ref=None, max_entries=5)


def test_too_much_uncompressed_is_refused_before_reading() -> None:
    with pytest.raises(SnapshotRefused, match="uncompressed"):
        index_tarball(
            _tarball({"a.txt": b"x" * 100}), owner="a", repo="b", ref=None, max_uncompressed=50
        )


def test_garbage_is_refused_and_a_bad_commit_is_dropped() -> None:
    with pytest.raises(SnapshotRefused, match="could not be read"):
        index_tarball(b"not a tarball", owner="a", repo="b", ref=None)
    snap = index_tarball(_tarball({"a": b"1"}, comment="nope"), owner="a", repo="b", ref=None)
    assert snap.commit == ""


# --- views through the reader --------------------------------------------------------------


async def test_the_overview() -> None:
    fake = _GitHub()
    page = await _reader(fake).read("https://github.com/acme/demo")
    assert page is not None and page.find == ""
    text = page.text
    assert text.startswith("# acme/demo\nhttps://github.com/acme/demo")
    assert "Widgets for everyone." in text and "Contribute to" not in text
    assert "default branch `main` · commit 0123456789ab" in text
    assert "| .py | 4 |" in text
    assert "- src/ (3 files" in text and "  - src/demo/ (3 files)" in text
    assert "- README.md (" in text
    assert "## README.md\n# Demo" in text
    assert "Fix the widget — https://github.com/acme/demo/commit/abc" in text
    assert "2026-10-01" in text
    assert "## Releases\n- 2026-09-01 · v1.0" in text
    assert "https://github.com/acme/demo/blob/main/<path>" in text
    assert f"https://github.com/acme/demo/commits/{SHA}.atom" in fake.calls


async def test_the_overview_survives_every_extra_failing() -> None:
    fake = _GitHub()
    for u in (
        "https://github.com/acme/demo",
        "https://github.com/acme/demo/releases.atom",
        f"https://github.com/acme/demo/commits/{SHA}.atom",
    ):
        fake.status[u] = 429
    page = await _reader(fake).read("https://github.com/acme/demo")
    assert page is not None
    assert "default branch · commit" in page.text
    assert "Recent commits" not in page.text
    assert "https://github.com/acme/demo/blob/HEAD/<path>" in page.text


async def test_a_long_readme_and_a_big_tree_are_capped() -> None:
    files = {"README.md": b"r" * (gh._README_CHARS + 50)}
    files |= {f"f{i:03}.txt": b"x" for i in range(gh._TREE_TOP_ENTRIES + 5)}
    files |= {f"d/sub{i:02}/x.txt": b"x" for i in range(gh._TREE_CHILD_DIRS + 2)}
    files |= {f"z{i}.ext{i}": b"x" for i in range(gh._LANG_ROWS + 2)}
    fake = _GitHub({"https://github.com/acme/demo/archive/HEAD.tar.gz": _tarball(files)})
    page = await _reader(fake).read("https://github.com/acme/demo")
    assert page is not None
    assert "[README truncated" in page.text
    assert "more entries — open a folder" in page.text
    assert "(+2 more folders)" in page.text
    assert "more) | | |" in page.text


async def test_an_empty_repo_overview() -> None:
    fake = _GitHub({"https://github.com/acme/demo/archive/HEAD.tar.gz": _tarball({})})
    page = await _reader(fake).read("https://github.com/acme/demo")
    assert page is not None
    assert "_Empty repository._" in page.text and "_No text files._" in page.text


async def test_a_folder_lists_every_entry_with_sizes() -> None:
    files = {f"pkg/m{i:03}.py": b"a = 1\nb = 2\n" for i in range(60)}
    files["pkg/README.md"] = b"pkg readme"
    files["pkg/sub/x.py"] = b"x"
    files["pkg/blob.bin"] = b"\x00"
    fake = _GitHub({"https://codeload.github.com/acme/demo/tar.gz/main": _tarball(files)})
    page = await _reader(fake).read("https://github.com/acme/demo/tree/main/pkg")
    assert page is not None
    assert page.url == "https://github.com/acme/demo/tree/main/pkg"
    assert page.text.startswith("# acme/demo/pkg\n")
    assert "1 folder(s), 62 file(s)" in page.text
    assert "- sub/ — 1 files, 1 B" in page.text
    assert "- m059.py — 12 B, 2 lines" in page.text
    assert "- blob.bin — 1 B, binary" in page.text
    assert page.text.count("\n- m") == 60  # not link-capped
    assert (
        "This folder has a README: web_fetch https://github.com/acme/demo/blob/main/pkg/README.md"
        in page.text
    )


async def test_the_root_tree_and_a_tree_that_names_a_file() -> None:
    fake = _GitHub({"https://codeload.github.com/acme/demo/tar.gz/main": _tarball(_REPO_FILES)})
    reader = _reader(fake)
    root = await reader.read("https://github.com/acme/demo/tree/main")
    assert root is not None and root.text.startswith("# acme/demo\n")  # the overview
    as_file = await reader.read("https://github.com/acme/demo/tree/main/setup.py")
    assert as_file is not None and "1  from setuptools import setup" in as_file.text


async def test_a_file_with_line_numbers_and_a_range() -> None:
    fake = _GitHub({"https://codeload.github.com/acme/demo/tar.gz/main": _tarball(_REPO_FILES)})
    reader = _reader(fake)
    page = await reader.read("https://github.com/acme/demo/blob/main/src/demo/core.py")
    assert page is not None
    assert page.title == "src/demo/core.py — acme/demo"
    assert "`src/demo/core.py` at ref `main` · commit 0123456789ab — 6 lines" in page.text
    assert "1  def widget():" in page.text and "5  def gadget():" in page.text
    ranged = await reader.read("https://github.com/acme/demo/blob/main/src/demo/core.py#L5-L6")
    assert ranged is not None
    assert "showing lines 5–6" in ranged.text
    assert "def widget" not in ranged.text and "5  def gadget():" in ranged.text
    clipped = await reader.read("https://github.com/acme/demo/blob/main/src/demo/core.py#L6-L99")
    assert clipped is not None and "showing lines 6–6" in clipped.text
    past = await reader.read("https://github.com/acme/demo/blob/main/src/demo/core.py#L90-L99")
    assert past is not None and "has 6 line(s), so there is no line 90" in past.text
    # A raw URL is served from a snapshot already here...
    raw = await reader.read("https://raw.githubusercontent.com/acme/demo/main/setup.py")
    assert raw is not None and "2  setup(name='demo')" in raw.text
    assert len(fake.archive_calls()) == 1  # one snapshot served all of it
    # ...but a file the snapshot cannot show is read raw, through the ordinary fetch.
    with pytest.raises(GitHubUnavailable) as exc:
        await reader.read("https://github.com/acme/demo/blob/main/assets/logo.png")
    assert exc.value.fallback
    assert exc.value.plain_url == (
        f"https://raw.githubusercontent.com/acme/demo/{SHA}/assets/logo.png"
    )
    assert (
        await reader.read("https://raw.githubusercontent.com/acme/demo/main/assets/logo.png")
        is None
    )


async def test_a_missing_path_shows_the_nearest_folder() -> None:
    fake = _GitHub({"https://codeload.github.com/acme/demo/tar.gz/main": _tarball(_REPO_FILES)})
    page = await _reader(fake).read("https://github.com/acme/demo/blob/main/src/demo/nope/x.py")
    assert page is not None
    assert "`src/demo/nope/x.py` does not exist" in page.text
    assert "Folder `src/demo`" in page.text
    top = await _reader(fake).read("https://github.com/acme/demo/blob/main/nowhere.py")
    assert top is not None and "Folder `(repository root)`" in top.text


async def test_find_searches_the_whole_repo_or_one_folder() -> None:
    fake = _GitHub()
    reader = _reader(fake)
    page = await reader.read("https://github.com/acme/demo", find="widget")
    assert page is not None and page.find == ""  # consumed by the search
    text = page.text
    assert "matching line(s) in 4 file(s)" in text
    assert "src/demo/core.py\n  1: def widget():\n  2: return 'widget'" in text
    assert "src/demo/util/helpers.py\n  2: WIDGET_COUNT = 3" in text
    sub = await reader.read("https://github.com/acme/demo/tree/HEAD/src/demo/util", find="widget")
    assert sub is not None and "docs/guide.md" not in sub.text and "helpers.py" in sub.text
    by_path = await reader.read("https://github.com/acme/demo", find="helpers")
    assert by_path is not None and "Paths matching (1):\n- src/demo/util/helpers.py" in by_path.text
    rx = await reader.read("https://github.com/acme/demo", find=r"def \w+\(", regex=True)
    assert rx is not None and "regex 'def \\w+\\('" in rx.text and "5: def gadget():" in rx.text
    none = await reader.read("https://github.com/acme/demo", find="zebra")
    assert none is not None and "No match for 'zebra'" in none.text
    blob = await reader.read("https://github.com/acme/demo/blob/HEAD/setup.py", find="setup")
    assert blob is not None and blob.find == "setup"  # a file keeps the page find


async def test_a_search_is_bounded() -> None:
    files = {"many.txt": b"hit\n" * (gh._SEARCH_MAX_LINES + 10)}
    fake = _GitHub({"https://github.com/acme/demo/archive/HEAD.tar.gz": _tarball(files)})
    page = await _reader(fake).read("https://github.com/acme/demo", find="hit")
    assert page is not None
    assert f"showing {gh._SEARCH_MAX_LINES}" in page.text
    assert "[+10 more matching line(s) not shown" in page.text


async def test_a_non_github_url_is_not_answered() -> None:
    assert await _reader(_GitHub()).read("https://example.com/x") is None


# --- default branch, slashed refs, failures -----------------------------------------------


async def test_the_default_branch_falls_back_through_the_chain() -> None:
    fake = _GitHub(
        {"https://codeload.github.com/acme/demo/tar.gz/refs/heads/master": _tarball(_REPO_FILES)}
    )
    page = await _reader(fake).read("https://github.com/acme/demo/blob/HEAD/setup.py")
    assert page is not None
    assert fake.calls[:4] == [
        "https://github.com/acme/demo/archive/HEAD.tar.gz",
        "https://codeload.github.com/acme/demo/tar.gz/HEAD",
        "https://codeload.github.com/acme/demo/tar.gz/refs/heads/main",
        "https://codeload.github.com/acme/demo/tar.gz/refs/heads/master",
    ]


async def test_a_slashed_ref_resolves_to_the_split_that_exists() -> None:
    fake = _GitHub(
        {"https://codeload.github.com/acme/demo/tar.gz/feature/x": _tarball(_REPO_FILES)}
    )
    reader = _reader(fake)
    page = await reader.read("https://github.com/acme/demo/blob/feature/x/setup.py")
    assert page is not None
    assert page.url == "https://github.com/acme/demo/blob/feature/x/setup.py"
    assert "ref `feature/x`" in page.text
    assert fake.calls[:2] == [
        "https://codeload.github.com/acme/demo/tar.gz/feature",
        "https://codeload.github.com/acme/demo/tar.gz/feature/x",
    ]
    # The next read under the same ref is served from the cache, skipping the dead split.
    n = len(fake.calls)
    again = await reader.read("https://github.com/acme/demo/tree/feature/x/src")
    assert again is not None and len(fake.calls) == n


async def test_a_missing_or_private_repo_is_a_clear_message() -> None:
    fake = _GitHub({})
    reader = _reader(fake)
    with pytest.raises(GitHubUnavailable) as exc:
        await reader.read("https://github.com/acme/secret/blob/nope/a.py")
    assert not exc.value.fallback
    assert "not found" in exc.value.message and "private" in exc.value.message
    assert "branch/tag" in exc.value.message
    # A bare two-segment URL with no archive may still be a real page: read it plainly.
    with pytest.raises(GitHubUnavailable) as bare:
        await reader.read("https://github.com/acme/secret")
    assert bare.value.fallback and "no public repository archive" in bare.value.message
    # The miss is remembered: a retry does not download again.
    n = len(fake.calls)
    with pytest.raises(GitHubUnavailable):
        await reader.read("https://github.com/acme/secret")
    assert len(fake.calls) == n


async def test_over_the_compressed_cap_is_refused_for_the_fallback() -> None:
    fake = _GitHub()
    reader = _reader(fake, max_compressed=100)
    with pytest.raises(GitHubUnavailable) as exc:
        await reader.read("https://github.com/acme/demo")
    assert exc.value.fallback and "too large to snapshot" in exc.value.message
    # Remembered: the retry refuses the same way without downloading again.
    n = len(fake.calls)
    with pytest.raises(GitHubUnavailable, match="too large to snapshot"):
        await reader.read("https://github.com/acme/demo")
    assert len(fake.calls) == n


async def test_a_blocked_archive_falls_back() -> None:
    fake = _GitHub()
    fake.status["https://github.com/acme/demo/archive/HEAD.tar.gz"] = 429
    with pytest.raises(GitHubUnavailable) as exc:
        await _reader(fake).read("https://github.com/acme/demo")
    assert exc.value.fallback and "could not be downloaded" in exc.value.message


async def test_a_redirect_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real SSRF guard (DNS included) on every hop of the archive download."""

    def resolve(host: str, *_a: object, **_k: object) -> list[tuple[Any, ...]]:
        ip = "10.0.0.5" if host == "evil.example" else "140.82.112.3"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    def real_guard(url: str, *, skip_dns: bool = False) -> None:
        _ = skip_dns
        original(url)

    original = fetch_mod.guard_public_host
    monkeypatch.setattr(fetch_mod, "guard_public_host", real_guard)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "github.com":
            return httpx.Response(
                302, headers={"location": "https://evil.example/acme/demo/tar.gz/HEAD"}
            )
        return httpx.Response(200, content=_tarball(_REPO_FILES))

    reader = GitHubReader(WebFetcher(transport=httpx.MockTransport(handler)))
    with pytest.raises(GitHubUnavailable) as exc:
        await reader.read("https://github.com/acme/demo")
    assert exc.value.fallback and "non-public" in exc.value.message
    assert all("evil.example" not in c for c in calls)


# --- cache + single-flight -----------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_the_snapshot_is_reused_then_expires() -> None:
    fake = _GitHub()
    clock = _Clock()
    reader = _reader(fake, clock=clock)
    await reader.read("https://github.com/acme/demo/blob/HEAD/setup.py")
    await reader.read("https://github.com/ACME/Demo/tree/HEAD/src")
    assert len(fake.archive_calls()) == 1
    clock.now += gh.CACHE_TTL_S + 1
    await reader.read("https://github.com/acme/demo/blob/HEAD/setup.py")
    assert len(fake.archive_calls()) == 2


async def test_the_overview_aliases_the_default_branch_name() -> None:
    fake = _GitHub()
    reader = _reader(fake)
    await reader.read("https://github.com/acme/demo")
    n = len(fake.archive_calls())
    page = await reader.read("https://github.com/acme/demo/blob/main/setup.py")
    assert page is not None and len(fake.archive_calls()) == n
    # Paging the overview again re-fetches nothing.
    calls = len(fake.calls)
    await reader.read("https://github.com/acme/demo")
    assert len(fake.calls) == calls


async def test_the_cache_is_bounded_by_count_and_text() -> None:
    archives = {
        f"https://codeload.github.com/acme/demo/tar.gz/r{i}": _tarball({"a.txt": b"x" * 10})
        for i in range(3)
    }
    fake = _GitHub(archives)
    reader = _reader(fake, max_snapshots=2)
    for i in range(3):
        await reader.read(f"https://github.com/acme/demo/blob/r{i}/a.txt")
    await reader.read("https://github.com/acme/demo/blob/r0/a.txt")  # evicted → downloaded again
    assert len(fake.archive_calls()) == 4
    one = index_tarball(_tarball({"a.txt": b"x" * 10}), owner="acme", repo="demo", ref="r0")
    assert one.mem_bytes > one.text_bytes  # the decoded str + bookkeeping, not raw bytes
    by_text = _reader(_GitHub(archives), max_text_bytes=one.mem_bytes * 3 // 2)
    await by_text.read("https://github.com/acme/demo/blob/r0/a.txt")
    await by_text.read("https://github.com/acme/demo/blob/r1/a.txt")
    assert len(by_text._cache) == 1


async def test_negative_entries_expire() -> None:
    fake = _GitHub({})
    clock = _Clock()
    reader = _reader(fake, clock=clock)
    with pytest.raises(GitHubUnavailable):
        await reader.read("https://github.com/acme/demo")
    clock.now += gh.NEGATIVE_TTL_S + 1
    n = len(fake.calls)
    with pytest.raises(GitHubUnavailable):
        await reader.read("https://github.com/acme/demo")
    assert len(fake.calls) > n


async def test_concurrent_reads_share_one_download() -> None:
    fake = _GitHub()
    fake.gate = asyncio.Event()
    reader = _reader(fake)
    first = asyncio.create_task(reader.read("https://github.com/acme/demo/blob/HEAD/setup.py"))
    second = asyncio.create_task(reader.read("https://github.com/acme/demo/tree/HEAD/src"))
    await asyncio.sleep(0.01)
    fake.gate.set()
    a, b = await asyncio.gather(first, second)
    assert a is not None and b is not None
    assert len(fake.archive_calls()) == 1


async def test_a_cancelled_caller_does_not_cancel_the_shared_download() -> None:
    fake = _GitHub()
    fake.gate = asyncio.Event()
    reader = _reader(fake)
    doomed = asyncio.create_task(reader.read("https://github.com/acme/demo/blob/HEAD/setup.py"))
    await asyncio.sleep(0.01)
    doomed.cancel()
    fake.gate.set()
    page = await reader.read("https://github.com/acme/demo/blob/HEAD/setup.py")
    assert page is not None
    assert len(fake.archive_calls()) == 1


# --- the web_fetch seams -------------------------------------------------------------------


class _Skips:
    def __init__(self, active: frozenset[str] = frozenset()) -> None:
        self.active = active
        self.recorded: list[tuple[str, str, str]] = []

    async def active_hosts(self) -> frozenset[str]:
        return self.active

    async def record(self, host: str, reason: str, url: str) -> None:
        self.recorded.append((host, reason, url))


def _handler(fake: _GitHub, skips: _Skips | None = None, *, github: bool = True) -> Any:
    fetcher = WebFetcher(transport=httpx.MockTransport(fake))
    return build_web_handlers(
        SearxngClient(""),
        fetcher,
        domain_skips=cast(DomainSkipRepo, skips) if skips is not None else None,
        github=GitHubReader(fetcher) if github else None,
    )["web_fetch"]


async def test_web_fetch_presents_a_snapshot_view_like_any_page() -> None:
    fake = _GitHub()
    out = await _handler(fake)({"url": "https://github.com/acme/demo/blob/HEAD/setup.py"}, _ctx())
    assert isinstance(out, ToolOutput)
    page = await _reader(_GitHub()).read("https://github.com/acme/demo/blob/HEAD/setup.py")
    assert page is not None
    expected = _present_result(
        window_text(page.text, url=page.url, title=page.title, tier="github"),
        url="https://github.com/acme/demo/blob/HEAD/setup.py",
        offset=0,
        find="",
        find_regex=False,
        outline_only=False,
        extract=False,
    )
    assert str(out) == str(expected)
    assert out.web_sources[0].url == "https://github.com/acme/demo/blob/HEAD/setup.py"
    assert out.result_brief


async def test_web_fetch_pages_finds_and_extracts_through_a_snapshot() -> None:
    fake = _GitHub()
    fetch = _handler(fake)
    found = await fetch({"url": "https://github.com/acme/demo", "find": "gadget"}, _ctx())
    assert "src/demo/core.py\n  5: def gadget():" in str(found)
    assert "No match for" not in str(found)
    extracted = await fetch(
        {"url": "https://github.com/acme/demo", "find": r"def (\w+)", "extract": True}, _ctx()
    )
    assert "regex 'def (\\w+)'" in str(extracted) and "1: def widget():" in str(extracted)
    in_file = await fetch(
        {"url": "https://github.com/acme/demo/blob/HEAD/src/demo/core.py", "find": "gadget"},
        _ctx(),
    )
    assert "found 1 match(es) for 'gadget'" in str(in_file)
    ext_file = await fetch(
        {
            "url": "https://github.com/acme/demo/blob/HEAD/src/demo/core.py",
            "find": r"def (\w+)",
            "extract": True,
        },
        _ctx(),
    )
    assert "[groups: widget]" in str(ext_file)
    outline = await fetch({"url": "https://github.com/acme/demo", "outline": True}, _ctx())
    assert "## File tree → offset" in str(outline) or "File tree → offset" in str(outline)
    paged = await fetch({"url": "https://github.com/acme/demo", "offset": 20}, _ctx())
    assert "[continued from offset 20" in str(paged)


async def test_web_fetch_says_a_missing_repo_plainly_without_a_plain_fetch() -> None:
    fake = _GitHub({})
    skips = _Skips()
    url = "https://github.com/acme/secret/blob/main/a.py"
    out = await _handler(fake, skips)({"url": url}, _ctx())
    assert "not found" in str(out) and "private" in str(out)
    assert url not in fake.calls  # no HTML fetch after it
    assert skips.recorded == []


async def test_web_fetch_falls_back_to_the_page_and_never_skip_lists_github() -> None:
    fake = _GitHub()
    fake.status["https://github.com/acme/demo/archive/HEAD.tar.gz"] = 429
    fake.status["https://github.com/acme/demo"] = 429
    skips = _Skips()
    out = await _handler(fake, skips)({"url": "https://github.com/acme/demo"}, _ctx())
    assert "GitHub repo snapshot unavailable" in str(out)
    assert "https://github.com/acme/demo" in fake.calls  # the plain fetch ran
    assert skips.recorded == []


async def test_web_fetch_fallback_reads_the_page_with_a_note() -> None:
    long_html = b"<html><head><title>acme/demo</title></head><body><p>" + b"word " * 300

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".tar.gz"):
            return httpx.Response(503)
        return httpx.Response(200, content=long_html, headers={"content-type": "text/html"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handler))
    fetch = build_web_handlers(SearxngClient(""), fetcher, github=GitHubReader(fetcher))[
        "web_fetch"
    ]
    out = await fetch({"url": "https://github.com/acme/demo"}, _ctx())
    assert isinstance(out, ToolOutput)
    assert "word word" in str(out)
    assert str(out).rstrip().endswith("this is the page read the ordinary way.]")
    assert out.web_sources


async def test_a_github_url_skips_the_skip_list_only_when_the_reader_is_wired() -> None:
    skips = _Skips(frozenset({"github.com"}))
    out = await _handler(_GitHub(), skips)({"url": "https://github.com/acme/demo"}, _ctx())
    assert str(out).startswith("# acme/demo")
    off = await _handler(_GitHub(), skips, github=False)(
        {"url": "https://github.com/acme/demo"}, _ctx()
    )
    assert "being skipped" in str(off)


async def test_an_issue_url_still_takes_the_plain_path() -> None:
    fake = _GitHub()
    await _handler(fake)({"url": "https://github.com/acme/demo/issues/1"}, _ctx())
    assert fake.calls == ["https://github.com/acme/demo/issues/1"]


def test_small_helpers() -> None:
    assert gh._fmt_size(3 * 1024 * 1024) == "3.0 MB"
    snap = index_tarball(_tarball({"a": b"1"}), owner="acme", repo="demo", ref=None)
    assert gh._clean_description("Widgets galore - acme/demo", snap) == "Widgets galore"


async def test_the_overview_reports_what_it_dropped() -> None:
    tb = _tarball({"ok.txt": b"fine\n"}, extra=_special_members)
    fake = _GitHub({"https://github.com/acme/demo/archive/HEAD.tar.gz": tb})
    reader = _reader(fake)
    page = await reader.read("https://github.com/acme/demo")
    assert page is not None
    dropped = "(4 symlink(s)/special file(s) not followed; 4 entr(ies) with unsafe paths dropped)"
    assert dropped in page.text
    # A direct snapshot lookup is a cache hit, not a second download.
    await reader._snapshot("acme", "demo", None)
    assert len(fake.archive_calls()) == 1


# --- review follow-ups ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # An encoded slash would carry `..` segments inside one "segment" past the check and,
        # once collapsed, aim the archive URL at another repository.
        "https://github.com/o/r/blob/a%2f..%2f..%2f..%2fevil%2fx%2ftar.gz%2fmain/f.py",
        "https://github.com/o/r/tree/a%2F..%2Fb",
        "https://raw.githubusercontent.com/o/r/main%2f..%2f..%2fevil/f.py",
        "https://github.com/solutions/devops",
        "https://github.com/readme/stories",
        "https://github.com/copilot/chat",
    ],
)
def test_encoded_slashes_and_site_sections_are_refused(url: str) -> None:
    assert parse_github_url(url) is None


async def test_ref_components_are_percent_encoded_in_every_url() -> None:
    assert gh._ref_path("feat/a b#c?d") == "feat/a%20b%23c%3Fd"
    fake = _GitHub(
        {"https://codeload.github.com/acme/demo/tar.gz/v1%231": _tarball({"a.txt": b"x"})}
    )
    page = await _reader(fake).read("https://github.com/acme/demo/blob/v1%231/a.txt")
    assert page is not None
    assert "https://codeload.github.com/acme/demo/tar.gz/v1%231" in fake.calls


def test_line_numbers_follow_newlines_only() -> None:
    text = "one\fstill one\r\ntwo\vstill two still two\rstill two\nthree\n"
    assert gh._lines(text) == [
        "one\fstill one",
        "two\vstill two still two\rstill two",
        "three",
    ]
    snap = index_tarball(_tarball({"f.txt": text.encode()}), owner="a", repo="b", ref="m")
    view = gh.render_blob(snap, snap.files["f.txt"], None)
    assert "— 3 lines" in view and "3  three" in view
    found = gh.render_search(snap, "", "three", regex=False)
    assert "  3: three" in found


def test_an_lfs_pointer_says_so() -> None:
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 3145728\n"
    snap = index_tarball(_tarball({"model.txt": pointer}), owner="a", repo="b", ref="m")
    view = gh.render_blob(snap, snap.files["model.txt"], None)
    assert "Git LFS pointer: the real content (3.0 MB) is stored outside" in view
    bare = index_tarball(
        _tarball({"p.txt": b"version https://git-lfs.github.com/spec/v1\n"}),
        owner="a",
        repo="b",
        ref="m",
    )
    assert "Git LFS pointer: the real content is stored" in gh.render_blob(
        bare, bare.files["p.txt"], None
    )


def test_a_catastrophic_pattern_is_stopped_not_run_forever() -> None:
    snap = index_tarball(
        _tarball({"evil.txt": b"a" * 40 + b"!\n", "z.txt": b"aaa\n"}), owner="a", repo="b", ref="m"
    )
    out = gh.render_search(snap, "", "(a|aa)+$", regex=True, match_timeout_s=0.01)
    assert "Search stopped early" in out


def test_the_search_budget_stops_a_long_search() -> None:
    snap = index_tarball(
        _tarball({f"f{i}.txt": b"hit\n" for i in range(5)}), owner="a", repo="b", ref="m"
    )
    ticks = iter(range(100))
    out = gh.render_search(
        snap, "", "hit", regex=False, budget_s=4.5, clock=lambda: float(next(ticks))
    )
    assert "Search stopped early" in out
    assert "matching line(s) in 2 file(s)" in out  # partial results are kept
    none = gh.render_search(snap, "", "zzz", regex=False, budget_s=0.5, clock=lambda: 0.0)
    assert "No match" in none and "stopped early" not in none


def test_a_match_past_the_scan_cap_is_not_searched_and_a_bad_pattern_is_reported() -> None:
    line = b"x" * (gh._SEARCH_SCAN_CHARS + 10) + b"needle\n"
    snap = index_tarball(_tarball({"f.txt": line}), owner="a", repo="b", ref="m")
    assert "No match" in gh.render_search(snap, "", "needle", regex=False)
    assert "Invalid regex for find" in gh.render_search(snap, "", "(unclosed", regex=True)


async def test_a_repo_search_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[object] = []
    real = asyncio.to_thread

    async def spy(fn: Callable[..., Any], *a: Any, **k: Any) -> Any:
        ran.append(fn)
        return await real(fn, *a, **k)

    monkeypatch.setattr(gh.asyncio, "to_thread", spy)
    page = await _reader(_GitHub()).read("https://github.com/acme/demo", find="widget")
    assert page is not None and gh.render_search in ran


async def test_snapshots_of_different_repos_are_built_one_at_a_time() -> None:
    archives = {
        "https://codeload.github.com/acme/one/tar.gz/main": _tarball({"a.txt": b"1"}),
        "https://codeload.github.com/acme/two/tar.gz/main": _tarball({"a.txt": b"2"}),
    }
    fake = _GitHub(archives)
    fake.gate = asyncio.Event()
    reader = _reader(fake)
    one = asyncio.create_task(reader.read("https://github.com/acme/one/blob/main/a.txt"))
    two = asyncio.create_task(reader.read("https://github.com/acme/two/blob/main/a.txt"))
    await asyncio.sleep(0.02)
    assert len(fake.archive_calls()) == 1  # the second waits for the first to finish
    fake.gate.set()
    assert all(p is not None for p in await asyncio.gather(one, two))
    assert len(fake.archive_calls()) == 2


async def test_a_download_has_an_overall_deadline() -> None:
    fake = _GitHub()
    fake.gate = asyncio.Event()  # never set: the archive never arrives
    with pytest.raises(GitHubUnavailable) as exc:
        await _reader(fake, deadline_s=0.05).read("https://github.com/acme/demo")
    assert exc.value.fallback and "took over" in exc.value.message


async def test_a_blocked_archive_is_remembered_and_rebuilt_per_hit() -> None:
    fake = _GitHub()
    fake.status["https://github.com/acme/demo/archive/HEAD.tar.gz"] = 429
    reader = _reader(fake)
    with pytest.raises(GitHubUnavailable) as first:
        await reader.read("https://github.com/acme/demo")
    n = len(fake.calls)
    with pytest.raises(GitHubUnavailable) as second:
        await reader.read("https://github.com/acme/demo")
    assert len(fake.calls) == n  # no second download inside the window
    assert second.value is not first.value
    assert second.value.message == first.value.message and second.value.fallback


async def test_expired_misses_are_pruned_on_insert() -> None:
    clock = _Clock()
    reader = _reader(_GitHub({}), clock=clock)
    with pytest.raises(GitHubUnavailable):
        await reader.read("https://github.com/acme/gone")
    clock.now += gh.NEGATIVE_TTL_S + 1
    with pytest.raises(GitHubUnavailable):
        await reader.read("https://github.com/acme/other")
    assert {k[1] for k in reader._negative} == {"other"}


def test_skipped_members_count_toward_the_uncompressed_cap() -> None:
    def big_unsafe(tf: tarfile.TarFile) -> None:
        info = tarfile.TarInfo("../outside.txt")
        info.size = 100
        tf.addfile(info, io.BytesIO(b"x" * 100))

    with pytest.raises(SnapshotRefused, match="uncompressed"):
        index_tarball(
            _tarball({}, extra=big_unsafe), owner="a", repo="b", ref=None, max_uncompressed=50
        )


async def test_web_fetch_reads_a_raw_file_plainly_when_no_snapshot_is_cached() -> None:
    fake = _GitHub()
    url = "https://raw.githubusercontent.com/acme/demo/main/setup.py"
    out = await _handler(fake)({"url": url}, _ctx())
    assert fake.calls == [url]  # the file itself, no tarball
    assert "snapshot" not in str(out)


async def test_web_fetch_reads_an_unshowable_file_from_raw() -> None:
    raw = f"https://raw.githubusercontent.com/acme/demo/{SHA}/assets/logo.png"
    fake = _GitHub()
    out = await _handler(fake)(
        {"url": "https://github.com/acme/demo/blob/HEAD/assets/logo.png"}, _ctx()
    )
    assert fake.calls[-1] == raw
    assert f"is read from {raw}" in str(out)


async def test_web_fetch_reads_a_bare_non_repo_path_as_a_page() -> None:
    fake = _GitHub({})
    out = await _handler(fake)({"url": "https://github.com/acme/notarepo"}, _ctx())
    assert "https://github.com/acme/notarepo" in fake.calls
    assert "no public repository archive" in str(out)
