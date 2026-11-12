"""The upstream GrowBot policy watch (jbrain.growbot.upstream) and its routes.

GitHub is two fakes: the commits Atom feed in GitHub's own shape, and a compare answer built
from the real upstream history (britcruise9/GrowBot 57d5322 → 4a2cfc2, trimmed).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import growbot_upstream
from jbrain.api.deps import current_debug_principal, owner_only
from jbrain.auth.service import PrincipalInfo
from jbrain.growbot import upstream
from jbrain.growbot.upstream import Pin, UpstreamWatch, is_policy_path, read_pin

PIN = "4a2cfc29ea5e79a2f1d5e9df30a602729d67b71b"
OLD = "57d5322dd4c9729604728df2083b5d9d81972feb"
NEW = "9e41b07c22d1a0e5b0c3d4f5a6b7c8d9e0f1a2b3"
OWNER = PrincipalInfo(id="00000000-0000-0000-0000-000000000001", kind="owner", label="owner")
DEBUG = PrincipalInfo(id="cap1", kind="capability_token", label="token")


def feed(*commits: tuple[str, str]) -> bytes:
    entries = "".join(
        f"""<entry>
  <id>tag:github.com,2008:Grit::Commit/{sha}</id>
  <link type="text/html" rel="alternate" href="https://github.com/britcruise9/GrowBot/commit/{sha}"/>
  <title>{title}</title>
  <updated>2026-11-09T10:00:00Z</updated>
  <author><name>britcruise9</name></author>
</entry>"""
        for sha, title in commits
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en-US">
  <id>tag:github.com,2008:/britcruise9/GrowBot/commits/main</id>
  <link type="text/html" rel="alternate" href="https://github.com/britcruise9/GrowBot/commits/main"/>
  <title>Recent Commits to GrowBot:main</title>
  <updated>2026-11-09T10:00:00Z</updated>
  {entries}
</feed>""".encode()


def compare(files: list[Any], ahead: int = 2) -> bytes:
    return json.dumps(
        {
            "status": "ahead",
            "ahead_by": ahead,
            "commits": [
                {
                    "sha": "a" * 40,
                    "commit": {
                        "message": "policies: diy-v2 walk\n\nlong body",
                        "committer": {"date": "2026-11-01T00:00:00Z"},
                    },
                },
                {
                    "sha": NEW,
                    "commit": {
                        "message": "normal-spin: retune cw",
                        "committer": {"date": "2026-11-09T00:00:00Z"},
                    },
                },
                "junk",
            ],
            "files": files,
        }
    ).encode()


FILES: list[Any] = [
    {"filename": "app/policies/diy-v2/walk_fwd.json", "status": "added"},
    {"filename": "app/policies/normal-spin-1005/cw.json", "status": "modified"},
    {"filename": "app/policy_drrma.json", "status": "removed"},
    {
        "filename": "policy/forward/forward_90mm.json",
        "previous_filename": "policy/forward/forward_85mm.json",
        "status": "renamed",
    },
    {"filename": "policy/Harsh_policies/X/step_1.pkl", "status": "added"},
    {"filename": "app/v3/next.html", "status": "modified"},
    {"filename": "README.md", "status": "modified"},
    {"no": "filename"},
    "junk",
]


class Box:
    """The watch's world: a clock, a settings row, the notifications, and what GitHub says."""

    def __init__(self, pin: str | None = PIN) -> None:
        self.now = datetime(2026, 11, 12, tzinfo=UTC)
        self.stored: object = None
        self.notes: list[tuple[str, str]] = []
        self.feed: bytes | Exception = feed((PIN, "app: live copy"))
        self.compare: bytes | Exception = compare(FILES)
        self.asked: list[str] = []
        self.pin = Pin("britcruise9", "GrowBot", pin) if pin else None

    async def fetch_feed(self, url: str) -> bytes:
        self.asked.append(url)
        if isinstance(self.feed, Exception):
            raise self.feed
        return self.feed

    async def fetch_json(self, url: str) -> bytes:
        self.asked.append(url)
        if isinstance(self.compare, Exception):
            raise self.compare
        return self.compare

    async def load(self) -> object:
        return self.stored

    async def save(self, record: dict[str, object]) -> None:
        self.stored = json.loads(json.dumps(record))

    def watch(self) -> UpstreamWatch:
        return UpstreamWatch(
            pin=lambda: self.pin,
            fetch_feed=self.fetch_feed,
            fetch_json=self.fetch_json,
            load=self.load,
            save=self.save,
            notify=lambda t, b: self.notes.append((t, b)),
            now=lambda: self.now,
        )

    def later(self, **kw: float) -> None:
        self.now += timedelta(**kw)


# -- the pin and the path rule ---------------------------------------------------------------


def test_read_pin(tmp_path: Path) -> None:
    (tmp_path / "upstream.json").write_text(
        json.dumps({"repo": "https://github.com/britcruise9/GrowBot", "commit": PIN})
    )
    pin = read_pin(tmp_path)
    assert pin == Pin("britcruise9", "GrowBot", PIN)
    assert pin is not None
    assert pin.url == "https://github.com/britcruise9/GrowBot"
    assert pin.slug == "britcruise9/GrowBot"


@pytest.mark.parametrize(
    "body",
    [
        None,
        "not json",
        "[]",
        json.dumps({"repo": 1, "commit": PIN}),
        json.dumps({"repo": "https://evil.example/a/b", "commit": PIN}),
        json.dumps({"repo": "https://github.com/a/b", "commit": "HEAD"}),
    ],
)
def test_read_pin_refuses_what_sync_does_not_write(tmp_path: Path, body: str | None) -> None:
    if body is not None:
        (tmp_path / "upstream.json").write_text(body)
    assert read_pin(tmp_path) is None


def test_read_pin_drops_a_git_suffix(tmp_path: Path) -> None:
    (tmp_path / "upstream.json").write_text(
        json.dumps({"repo": "https://github.com/britcruise9/GrowBot.git", "commit": PIN})
    )
    assert read_pin(tmp_path) == Pin("britcruise9", "GrowBot", PIN)


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("app/policies/diy-v1/walk_fwd.json", True),
        ("app/policies/spin-v4-0911.json", True),
        ("app/policy_85mm.json", True),
        ("policy/forward/forward_85mm.json", True),
        ("app/growbot_policy.js", False),
        ("app/policy-residual.mjs", False),
        ("app/v3/policy_x.json", False),
        ("policy/Harsh_policies/DR_RMA_EXPORT/DR_RMA_checkpoint/step_1310720.pkl", False),
        ("app/v3/body.json", False),
    ],
)
def test_is_policy_path(path: str, want: bool) -> None:
    assert is_policy_path(path) is want


# -- the check -------------------------------------------------------------------------------


def test_up_to_date_costs_one_feed_read_and_no_notification() -> None:
    box = Box()
    status = asyncio.run(box.watch().check())
    assert status.state == "current"
    assert status.head == PIN
    assert status.policies == []
    assert box.asked == ["https://github.com/britcruise9/GrowBot/commits/main.atom"]
    assert box.notes == []
    assert box.stored is not None


def test_new_policies_are_listed_and_notified_once() -> None:
    box = Box()
    box.feed = feed((NEW, "normal-spin: retune cw"), ("a" * 40, "diy-v2"), (PIN, "live copy"))
    watch = box.watch()
    status = asyncio.run(watch.check())
    assert status.state == "behind"
    assert status.ahead_by == 2
    assert [(p.path, p.change) for p in status.policies or []] == [
        ("app/policies/diy-v2/walk_fwd.json", "new"),
        ("app/policies/normal-spin-1005/cw.json", "changed"),
        ("app/policy_drrma.json", "removed"),
        ("policy/forward/forward_90mm.json", "renamed"),
    ]
    assert status.policies is not None
    assert status.policies[3].previous_path == "policy/forward/forward_85mm.json"
    assert status.other_files == 3
    assert [c.title for c in status.commits] == ["normal-spin: retune cw", "policies: diy-v2 walk"]
    assert status.compare_url.endswith(f"/compare/{PIN[:12]}...{NEW[:12]}")
    assert box.asked[-1] == (
        f"https://api.github.com/repos/britcruise9/GrowBot/compare/{PIN}...{NEW}"
    )
    assert len(box.notes) == 1
    title, body = box.notes[0]
    assert title == "New GrowBot walks and spins"
    assert "4 new or changed body policies" in body
    assert "diy-v2/walk_fwd.json, normal-spin-1005/cw.json, policy_drrma.json and 1 more" in body
    assert "Ops → Update" in body

    # The next scheduled check reuses the answered compare and does not notify again.
    box.later(hours=13)
    asked = len(box.asked)
    again = asyncio.run(watch.check())
    assert again.policies == status.policies
    assert box.asked[asked:] == ["https://github.com/britcruise9/GrowBot/commits/main.atom"]
    assert len(box.notes) == 1


def test_one_policy_reads_singular() -> None:
    box = Box()
    box.feed = feed((NEW, "x"), (PIN, "y"))
    box.compare = compare([{"filename": "app/policies/x/walk.json", "status": "added"}], 1)
    asyncio.run(box.watch().check())
    assert "1 new or changed body policy: x/walk.json." in box.notes[0][1]


def test_moved_without_policies_does_not_notify() -> None:
    box = Box()
    box.feed = feed((NEW, "readme"), (PIN, "y"))
    box.compare = compare([{"filename": "README.md", "status": "modified"}], 1)
    status = asyncio.run(box.watch().check())
    assert status.state == "behind"
    assert status.policies == []
    assert box.notes == []


def test_a_newer_upstream_notifies_again() -> None:
    box = Box()
    box.feed = feed((NEW, "x"), (PIN, "y"))
    watch = box.watch()
    asyncio.run(watch.check())
    newer = "b" * 40
    box.feed = feed((newer, "z"), (NEW, "x"), (PIN, "y"))
    box.later(hours=13)
    status = asyncio.run(watch.check())
    assert status.head == newer
    assert status.notified_head == newer
    assert len(box.notes) == 2


def test_check_now_is_throttled_and_the_schedule_skips_a_fresh_result() -> None:
    box = Box()
    watch = box.watch()
    asyncio.run(watch.check())
    asked = len(box.asked)
    box.later(seconds=30)
    asyncio.run(watch.check(force=True))
    box.later(hours=1)
    asyncio.run(watch.check())
    assert len(box.asked) == asked
    asyncio.run(watch.check(force=True))
    assert len(box.asked) == asked + 1


def test_compare_failure_still_says_upstream_moved_from_the_feed() -> None:
    box = Box()
    box.feed = feed((NEW, "two"), ("a" * 40, "one"), (PIN, "pin"))
    box.compare = RuntimeError("403 rate limited")
    status = asyncio.run(box.watch().check())
    assert status.state == "behind"
    assert status.policies is None
    assert status.ahead_by == 2
    assert [c.title for c in status.commits] == ["two", "one"]
    assert "wouldn't list the changed files" in status.error
    assert box.notes == []


def test_compare_failure_with_the_pin_off_the_feed() -> None:
    box = Box()
    box.feed = feed((NEW, "two"))
    box.compare = b"not json"
    status = asyncio.run(box.watch().check())
    assert status.ahead_by is None
    assert [c.sha for c in status.commits] == [NEW]


@pytest.mark.parametrize(
    "answer",
    [json.dumps({"message": "Not Found"}).encode(), b"[]"],
)
def test_compare_without_a_file_list_is_unreadable(answer: bytes) -> None:
    box = Box()
    box.feed = feed((NEW, "x"), (PIN, "y"))
    box.compare = answer
    status = asyncio.run(box.watch().check())
    assert status.policies is None


def test_a_failed_feed_keeps_the_last_good_result() -> None:
    box = Box()
    box.feed = feed((NEW, "x"), (PIN, "y"))
    watch = box.watch()
    good = asyncio.run(watch.check())
    box.feed = RuntimeError("down")
    box.later(hours=13)
    status = asyncio.run(watch.check())
    assert status.policies == good.policies
    assert status.head == NEW
    assert status.checked_at == box.now
    assert status.error.startswith("GitHub didn't answer")
    assert len(box.notes) == 1


def test_a_failed_first_check_is_unknown() -> None:
    box = Box()
    box.feed = RuntimeError("down")
    status = asyncio.run(box.watch().check())
    assert status.state == "unknown"
    assert status.pinned == PIN
    assert status.error


def test_an_empty_feed_is_a_failed_check() -> None:
    box = Box()
    box.feed = feed()
    status = asyncio.run(box.watch().check())
    assert status.state == "unknown"
    assert "empty" in status.error


def test_no_pin_says_so() -> None:
    box = Box(pin=None)
    status = asyncio.run(box.watch().check())
    assert status.state == "unknown"
    assert "Ops → Update" in status.error
    assert box.asked == []


def test_a_garbled_stored_result_reads_as_never_checked() -> None:
    box = Box()
    box.stored = {"state": "sideways"}
    assert asyncio.run(box.watch().last()).checked_at is None
    box.stored = "nope"
    assert asyncio.run(box.watch().last()).state == "unknown"


def test_the_loop_survives_a_failing_check(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Boom:
        async def check(self) -> None:
            calls.append("check")
            raise RuntimeError("boom")

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(upstream.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(upstream.run_upstream_loop(Boom(), first_delay=5))  # type: ignore[arg-type]
    assert sleeps == [5, 3600.0, 3600.0]
    assert calls == ["check", "check"]


# -- the routes ------------------------------------------------------------------------------


def _app(box: Box | None, *, auth: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(growbot_upstream.router, prefix="/api")
    app.include_router(growbot_upstream.debug_router, prefix="/api")
    if box is not None:
        app.state.growbot_upstream = box.watch()
    app.dependency_overrides[owner_only] = (lambda: OWNER) if auth else _refuse
    app.dependency_overrides[current_debug_principal] = (lambda: DEBUG) if auth else _refuse
    return app


def _refuse() -> PrincipalInfo:
    raise HTTPException(status_code=401, detail="unauthorized")


@pytest.mark.parametrize("base", ["/api/growbot/upstream", "/api/debug/growbot/upstream"])
def test_routes_read_and_check(base: str) -> None:
    box = Box()
    box.feed = feed((NEW, "x"), (PIN, "y"))
    client = TestClient(_app(box))
    assert client.get(base).json()["checked_at"] is None
    checked = client.post(f"{base}/check").json()
    assert checked["state"] == "behind"
    assert len(checked["policies"]) == 4
    assert client.get(base).json()["head"] == NEW


def test_routes_say_when_the_watch_is_not_running() -> None:
    client = TestClient(_app(None))
    assert client.get("/api/growbot/upstream").status_code == 503
    assert client.post("/api/debug/growbot/upstream/check").status_code == 503


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/growbot/upstream"),
        ("POST", "/api/growbot/upstream/check"),
        ("GET", "/api/debug/growbot/upstream"),
        ("POST", "/api/debug/growbot/upstream/check"),
    ],
)
def test_routes_need_the_owner_or_a_debug_token(method: str, path: str) -> None:
    box = Box()
    r = TestClient(_app(box, auth=False)).request(method, path)
    assert r.status_code == 401
    assert box.asked == []


def test_short_names_keep_where_a_contributor_export_lives() -> None:
    assert upstream._short("app/policies/diy-v1/walk_fwd.json") == "diy-v1/walk_fwd.json"
    assert upstream._short("app/policy_85mm.json") == "policy_85mm.json"
    assert upstream._short("policy/forward/forward_85mm.json") == "policy/forward/forward_85mm.json"
