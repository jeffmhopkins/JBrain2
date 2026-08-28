"""Owner-facing moltbook.com links (docs/plans/JMOLT_HARDENING_PLAN.md).

The owner asked the observer to link them to a post and got "I'm unable to provide a link":
the observer had the id all along and no way to turn it into a URL. It now returns one, from
the same builder the PWA activity feed uses, so both surfaces agree.

Two properties matter more than the happy path. The base is DERIVED from the pinned API base,
so a link can never point off moltbook.com. And a link is only built when the id is on a safe
charset and the URL shape is one that actually resolves — a 404 handed to the owner as an
answer is worse than an honest "there is no page for this".
"""

from __future__ import annotations

from jbrain.agent.jmoltobservetools import _ledger_url
from jbrain.web.moltbook import BASE_URL, WEB_BASE_URL, moltbook_web_url


def test_the_web_base_is_derived_from_the_pinned_api_base() -> None:
    """Never configurable, never model-supplied — that is the whole guarantee."""
    assert WEB_BASE_URL == "https://www.moltbook.com"
    assert BASE_URL.startswith(WEB_BASE_URL)


def test_a_post_links_to_itself_and_a_comment_to_its_thread() -> None:
    assert moltbook_web_url("post", {}, "abc-123") == f"{WEB_BASE_URL}/post/abc-123"
    assert moltbook_web_url("comment", {"post_id": "p-9"}) == f"{WEB_BASE_URL}/post/p-9"


def test_a_follow_links_to_the_profile() -> None:
    assert moltbook_web_url("follow", {"name": "Luna24"}) == f"{WEB_BASE_URL}/u/Luna24"
    assert moltbook_web_url("subscribe", {"name": "general"}) == f"{WEB_BASE_URL}/u/general"


def test_a_comment_vote_links_nowhere_rather_than_to_a_404() -> None:
    """The target is a comment id with no stored parent post, and /post/{comment} 404s. A post
    vote does resolve."""
    assert moltbook_web_url("vote", {"target_id": "t-1", "comment": True}) is None
    assert moltbook_web_url("vote", {"target_id": "t-1"}) == f"{WEB_BASE_URL}/post/t-1"


def test_a_crafted_id_cannot_bend_the_url_off_moltbook() -> None:
    """Ids reach here one hop from jmolt's own text, which is one hop from a hostile thread.
    Anything off the safe charset yields no link at all — never a redirected one."""
    for hostile in (
        "../../evil",
        "x/../../u/admin",
        "https://evil.example.com",
        "a b",
        "id?next=//evil",
        "id#frag",
        "",
    ):
        assert moltbook_web_url("post", {}, hostile) is None, hostile
        assert moltbook_web_url("comment", {"post_id": hostile}) is None, hostile
    assert moltbook_web_url("follow", {"name": "a/b"}) is None


def test_an_unknown_kind_has_no_link() -> None:
    assert moltbook_web_url("profile_update", {"bio": "x"}) is None
    assert moltbook_web_url("post", {}, None) is None


# ---- the action ledger is thinner than the outbox, and two kinds cannot be linked ----


def test_a_ledger_comment_links_to_its_post() -> None:
    assert _ledger_url("stage_comment", "p-1") == f"{WEB_BASE_URL}/post/p-1"
    assert _ledger_url("publish_comment", "p-1") == f"{WEB_BASE_URL}/post/p-1"


def test_a_ledger_post_does_not_link_because_its_target_is_a_submolt() -> None:
    """`stage_post` records the SUBMOLT it went to, not a post id. /post/general would be a
    404 dressed as an answer; the real link is in `action=outbox`, which has the moltbook id."""
    assert _ledger_url("stage_post", "general") is None


def test_a_ledger_vote_does_not_link_because_the_target_is_ambiguous() -> None:
    """The ledger does not record whether a vote was cast on a post or a comment, and a
    comment id under /post/ 404s. Without that bit, no honest link exists."""
    assert _ledger_url("stage_vote", "t-1") is None


def test_a_ledger_follow_links_to_the_profile() -> None:
    assert _ledger_url("stage_follow", "Luna24") == f"{WEB_BASE_URL}/u/Luna24"
    assert _ledger_url("stage_subscribe", "general") == f"{WEB_BASE_URL}/u/general"
