"""The simulator's Moltbook: reads from a corpus, writes believed, no way out to the network.

The load-bearing tests here are the two that would invalidate every measurement taken against
the simulator if they broke: that it cannot reach the real platform, and that a believed write
is visible to a later read in the same night — the condition that produced the self-reply.
"""

from datetime import UTC, datetime

import httpx
import pytest

from jbrain.agent.jmolt_sim_client import SimCorpus, SimMoltbookClient
from jbrain.web.fetch import WebFetchError
from jbrain.web.moltbook import MoltbookError

pytestmark = pytest.mark.anyio


def _corpus() -> SimCorpus:
    posts = {
        "p1": {
            "id": "p1",
            "title": "On the shape of a week",
            "content": "A body about weeks.",
            "author": {"name": "otheragent"},
            "created_at": "2026-08-29T07:00:00+00:00",
            "score": 4,
            "submolt": {"name": "philosophy"},
        },
        "p2": {
            "id": "p2",
            "title": "A second thing",
            "content": "Unrelated.",
            "author": {"name": "thirdagent"},
            "created_at": "2026-08-29T07:05:00+00:00",
            "score": 1,
            "submolt": {"name": "philosophy"},
        },
    }
    return SimCorpus(
        handle="jmolt",
        home={"submolts": ["philosophy"], "suggested_actions": ["post now"]},
        me={"name": "jmolt", "recentPosts": [{"id": "old1", "title": "Last night"}]},
        submolts={"submolts": [{"name": "philosophy"}]},
        posts=posts,
        comments={"p1": [{"id": "c1", "content": "hi", "author": {"name": "otheragent"}}]},
        profiles={"otheragent": {"name": "otheragent", "bio": "another agent"}},
        feed={"hot": ["p1", "p2"], "new": ["p2", "p1"]},
        submolt_feed={"philosophy": ["p1", "p2"]},
    )


def _client(**kw) -> SimMoltbookClient:
    at = datetime(2026, 8, 29, 7, 30, tzinfo=UTC)
    return SimMoltbookClient(_corpus(), clock=lambda: at, **kw)


async def test_it_holds_no_credential() -> None:
    """The simulator cannot produce an auth header, so nothing it did could be authenticated
    as jmolt even if it escaped."""
    with pytest.raises(MoltbookError):
        await _client()._auth_header()


async def test_its_transport_refuses_to_carry_anything() -> None:
    """Proved on its own rather than through a request, because the base URL's scheme is
    refused by the SSRF guard first — one fence must not hide the state of the other."""
    c = _client()
    with pytest.raises(MoltbookError):
        await c._transport.handle_async_request(httpx.Request("POST", "https://moltbook.com/x"))


async def test_bypassing_the_override_cannot_reach_the_network() -> None:
    """If a future code path called the real `_request`, it dies at the URL guard — the base
    URL is not an http(s) one — instead of quietly posting under jmolt's live key."""
    c = _client()
    with pytest.raises((MoltbookError, WebFetchError)):
        await super(SimMoltbookClient, c)._request("GET", "/home", authed=False)


async def test_reads_come_back_in_the_shape_the_tools_render() -> None:
    c = _client()
    feed = await c.feed(sort="hot")
    assert [p["id"] for p in feed["posts"]] == ["p1", "p2"]
    assert (await c.feed(sort="new"))["posts"][0]["id"] == "p2"
    assert (await c.post("p1"))["title"] == "On the shape of a week"
    assert (await c.submolt_feed("philosophy"))["posts"][0]["id"] == "p1"
    assert (await c.profile("otheragent"))["bio"] == "another agent"
    assert (await c.comments("p1"))["comments"][0]["id"] == "c1"
    assert (await c.search("weeks"))["results"][0]["id"] == "p1"
    assert await c.status() == "claimed"


async def test_the_handle_is_known_before_the_first_call() -> None:
    """The real client learns its handle from an authed call; the simulator has none. Without
    this the thread renderer cannot mark jmolt's own comments as its own — the fix that
    stopped it writing in another agent's voice."""
    assert _client().handle == "jmolt"


async def test_a_believed_comment_is_visible_to_the_next_read() -> None:
    """The self-reply condition, reproduced. Hiding this would make the simulator unable to
    show the bug it exists to study."""
    c = _client()
    written = await c.create_comment("p1", "my own fresh thought")
    assert written["comment"]["id"].startswith("sim_")
    back = await c.comments("p1")
    assert [x["content"] for x in back["comments"]][-1] == "my own fresh thought"
    assert back["comments"][-1]["author"]["name"] == "jmolt"


async def test_a_believed_post_is_readable_and_lands_in_the_account_history() -> None:
    """`me_history` is what reconcile-before-publish (M23) reads, so a believed post has to
    appear there or the simulator would exercise a double-post path the live night never hits."""
    c = _client()
    made = await c.create_post("philosophy", "A new title", content="body")
    pid = made["post"]["id"]
    assert (await c.post(pid))["title"] == "A new title"
    assert (await c.me_history())[0]["id"] == pid


async def test_every_write_kind_is_recorded_in_order() -> None:
    c = _client()
    await c.create_post("philosophy", "t", content="b")
    await c.create_comment("p1", "c")
    await c.vote("p1", up=True)
    await c.follow("otheragent")
    await c.subscribe("philosophy")
    await c.update_profile("a description")
    assert [w.kind for w in c.writes] == [
        "post",
        "comment",
        "vote",
        "follow",
        "subscribe",
        "profile",
    ]
    assert [w.seq for w in c.writes] == [1, 2, 3, 4, 5, 6]


async def test_a_route_the_corpus_does_not_cover_is_an_honest_error() -> None:
    """A missing route must look like the platform refusing, not like an empty result — an
    empty result would be scored as jmolt choosing not to engage."""
    c = _client()
    with pytest.raises(MoltbookError):
        await c.post("nosuchpost")
    with pytest.raises(MoltbookError):
        await c.profile("nobody")
    with pytest.raises(MoltbookError):
        await c.submolt_feed("nosuchsubmolt")


async def test_the_production_caps_still_apply() -> None:
    """The simulator subclasses the real client precisely so M12 truncation is not
    reimplemented. A corpus item over the cap must come back truncated."""
    corpus = _corpus()
    corpus.posts["p1"]["content"] = "x" * 5000
    c = SimMoltbookClient(corpus, max_item_chars=100)
    body = (await c.post("p1"))["content"]
    assert body.endswith("…[truncated]") and len(body) < 200


async def test_paging_hands_back_a_cursor_that_works() -> None:
    c = _client()
    first = await c.feed(sort="hot", limit=1)
    assert first["has_more"] and first["next_cursor"]
    second = await c.feed(sort="hot", limit=1, cursor=first["next_cursor"])
    assert [p["id"] for p in second["posts"]] == ["p2"]
    assert not second.get("has_more")
