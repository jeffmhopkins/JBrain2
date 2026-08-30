"""Snapshotting the live platform into a simulator corpus (JMOLT_LEDGER_ENGINE_PLAN.md, S1).

The load-bearing test is that the harvest never writes. "We only call the read methods" is the
kind of claim that stops being true quietly, so it is asserted against a client whose every
write method raises rather than trusted to review.
"""

import httpx
import pytest

from jbrain.agent.jmolt_harvest import MAX_PROFILES, harvest_corpus
from jbrain.web.moltbook import MoltbookClient, MoltbookError

pytestmark = pytest.mark.anyio

WRITE_METHODS = (
    "create_post",
    "create_comment",
    "vote",
    "follow",
    "subscribe",
    "update_profile",
    "register",
    "submit_verify",
)


def _post(pid: str, author: str, submolt: str = "philosophy") -> dict:
    return {
        "id": pid,
        "title": f"title {pid}",
        "content": "body",
        "author": {"name": author},
        "created_at": "2026-08-29T07:00:00+00:00",
        "submolt": {"name": submolt},
    }


def _client(handler, **kw) -> MoltbookClient:
    async def _key() -> tuple[str, str]:
        return "moltbook_key123456", "jmolt"

    return MoltbookClient(_key, transport=httpx.MockTransport(handler), **kw)


def _platform(**overrides):
    """A little Moltbook over MockTransport, so the harvest is exercised through the real
    client — its capping, its sorts, its paging — rather than against a stub of itself."""
    routes: dict[str, dict] = {
        "/home": {"submolts": ["philosophy"]},
        "/submolts": {"submolts": [{"name": "philosophy"}, {"name": "meta"}]},
        "/agents/me": {"name": "jmolt", "recentPosts": []},
        "/feed": {"posts": [_post("p1", "alice"), _post("p2", "bob")]},
        "/posts": {"posts": [_post("p1", "alice")]},
    }
    routes.update(overrides)

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.replace("/api/v1", "")
        if path.endswith("/comments"):
            pid = path.split("/")[2]
            return httpx.Response(
                200, json={"comments": [{"id": f"c-{pid}", "content": "hi", "author": "carol"}]}
            )
        if path == "/agents/profile":
            name = req.url.params.get("name", "")
            return httpx.Response(200, json={"name": name, "bio": f"{name}'s bio"})
        if path in routes:
            return httpx.Response(200, json=routes[path])
        return httpx.Response(404, json={"error": "no"})

    return handler


async def test_the_harvest_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(_platform())

    async def _refuse(*_a, **_kw):
        raise AssertionError("the harvest called a WRITE method")

    for name in WRITE_METHODS:
        monkeypatch.setattr(client, name, _refuse, raising=True)
    corpus = await harvest_corpus(client)
    assert corpus.posts  # and it still produced a corpus


async def test_it_records_each_sort_as_an_ordering_over_one_post_table() -> None:
    """The same post appears in `hot` and in `new`; storing it twice would let a simulated
    read return two different versions of one post."""
    corpus = await harvest_corpus(_client(_platform()))
    assert set(corpus.feed) == {"hot", "new"}
    assert corpus.feed["hot"] == ["p1", "p2"]
    assert sorted(corpus.posts) == ["p1", "p2"]
    assert corpus.submolt_feed["philosophy"] == ["p1"]


async def test_it_captures_the_threads_and_the_authors() -> None:
    """Without the comments a corpus cannot show a self-reply; without the profiles, a
    considered reply becomes a failed tool call."""
    corpus = await harvest_corpus(_client(_platform()))
    assert corpus.comments["p1"][0]["id"] == "c-p1"
    assert set(corpus.profiles) >= {"alice", "bob", "carol"}
    assert "jmolt" not in corpus.profiles  # its own profile is `me`, not a peer's


async def test_a_failing_read_is_skipped_rather_than_losing_the_snapshot() -> None:
    """A harvest that dies on the first 404 never produces a corpus at all."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/home"):
            return httpx.Response(500, json={"error": "down"})
        return _platform()(req)

    corpus = await harvest_corpus(_client(handler))
    assert corpus.home == {}
    assert corpus.posts and corpus.feed["hot"]  # the rest of the snapshot survived


async def test_the_fan_out_is_bounded() -> None:
    """The platform is the stated threat model; an unbounded walk of it is a fetch loop
    someone else steers."""
    many = {"posts": [_post(f"p{i}", f"agent{i}") for i in range(200)]}
    corpus = await harvest_corpus(_client(_platform(**{"/feed": many})))
    assert len(corpus.profiles) <= MAX_PROFILES
    assert len(corpus.comments) <= 25


async def test_the_snapshot_round_trips_through_json() -> None:
    """A corpus is stored and replayed later; a shape that cannot round-trip is one that
    silently changes between the harvest and the night."""
    from jbrain.agent.jmolt_sim_client import SimCorpus

    corpus = await harvest_corpus(_client(_platform()), handle="DaveFromSpace")
    back = SimCorpus.from_json(corpus.to_json())
    assert back.handle == "DaveFromSpace"
    assert back.posts == corpus.posts
    assert back.feed == corpus.feed
    assert back.captured_at == corpus.captured_at


async def test_a_platform_error_class_is_still_what_callers_see() -> None:
    """The harvest swallows platform errors; nothing else. A programming error must not be
    laundered into a quietly empty corpus."""

    def handler(_req: httpx.Request) -> httpx.Response:
        raise RuntimeError("not a MoltbookError")

    with pytest.raises((RuntimeError, MoltbookError)):
        await harvest_corpus(_client(handler))
