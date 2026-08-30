"""Snapshot the live Moltbook into a corpus a simulated night can run against.

The simulator is only as good as what it serves (docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S1).
A hand-written corpus measures a platform we invented; this one measures the platform jmolt
actually woke up to. A snapshot is deterministic and re-runnable, so the same night can be
replayed against different engines and the difference is the engine.

**Reads only.** The harvest calls the read methods on `MoltbookClient` and nothing else —
never `create_post`, `create_comment`, `vote`, `follow`, `subscribe` or `update_profile`. That
is asserted by a test that hands it a client whose every write method raises, because "we only
call the read ones" is the kind of claim that stops being true quietly.

It is deliberately bounded. The platform is the stated threat model and a harvest walking it
unboundedly is a fetch loop an adversary steers; every fan-out here has a cap, and a read that
fails is skipped rather than aborting the snapshot — a corpus missing one thread is still a
corpus, and a harvest that dies on the first 404 never produces one at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from jbrain.agent.jmolt_sim_client import SimCorpus
from jbrain.web.moltbook import MoltbookClient, MoltbookError

log = structlog.get_logger(__name__)

# Fan-out caps. Wide enough that a night's reading is representable, bounded so a hostile or
# merely enormous platform cannot turn a snapshot into an unbounded crawl.
MAX_FEED_POSTS = 30
MAX_SUBMOLTS = 8
MAX_THREADS = 25
MAX_PROFILES = 25
FEED_SORTS = ("hot", "new")


def _author(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    who = item.get("author")
    if isinstance(who, dict):
        return str(who.get("name") or "")
    return str(who or item.get("author_name") or "")


async def harvest_corpus(client: MoltbookClient, *, handle: str = "") -> SimCorpus:
    """One snapshot of the platform as it stands. Never raises on a platform error: a partial
    corpus is a corpus, and the caller can see what is missing from what it holds."""
    corpus = SimCorpus(
        handle=handle or client.handle or "jmolt",
        captured_at=datetime.now(UTC).isoformat(),
    )

    async def _try(what: str, call: Any) -> Any:
        try:
            return await call
        except MoltbookError as exc:
            log.warning("jmolt_harvest.skipped", what=what, status=exc.status)
            return None

    corpus.home = await _try("home", client.home()) or {}
    corpus.submolts = await _try("submolts", client.submolts()) or {}
    corpus.me = await _try("me", client.me()) or {}

    # The feeds, per sort, recorded as ORDERINGS over one shared post table — so the same
    # corpus answers `hot` and `new` differently without storing a post twice.
    for sort in FEED_SORTS:
        data = await _try(f"feed:{sort}", client.feed(sort=sort, limit=MAX_FEED_POSTS))
        corpus.feed[sort] = _index(corpus, data)

    for name in _submolt_names(corpus)[:MAX_SUBMOLTS]:
        data = await _try(f"submolt:{name}", client.submolt_feed(name, limit=MAX_FEED_POSTS))
        if data is not None:
            corpus.submolt_feed[name] = _index(corpus, data)

    # Threads over what the feeds surfaced: the comments are where jmolt's own voice and
    # everyone else's meet, so a corpus without them cannot show a self-reply.
    for post_id in list(corpus.posts)[:MAX_THREADS]:
        data = await _try(f"comments:{post_id}", client.comments(post_id, sort="new"))
        if isinstance(data, dict) and isinstance(data.get("comments"), list):
            corpus.comments[post_id] = [dict(c) for c in data["comments"] if isinstance(c, dict)]

    for name in _authors(corpus)[:MAX_PROFILES]:
        data = await _try(f"profile:{name}", client.profile(name))
        if data is not None:
            corpus.profiles[name] = dict(data)
    return corpus


def _index(corpus: SimCorpus, data: Any) -> list[str]:
    """Fold a feed response into the shared post table and return its ordering."""
    if not isinstance(data, dict):
        return []
    order: list[str] = []
    for item in data.get("posts") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "")
        if not pid:
            continue
        corpus.posts.setdefault(pid, dict(item))
        order.append(pid)
    return order


def _submolt_names(corpus: SimCorpus) -> list[str]:
    """The submolts to snapshot: what the account is subscribed to first, then whatever the
    feed showed — jmolt reads its subscriptions, and a corpus that only held the global feed
    would not reproduce a night spent in one room."""
    names: list[str] = []
    for source in (corpus.home.get("submolts"), corpus.submolts.get("submolts")):
        for entry in source or []:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    for post in corpus.posts.values():
        sub = post.get("submolt")
        name = sub.get("name") if isinstance(sub, dict) else sub
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _authors(corpus: SimCorpus) -> list[str]:
    """Everyone whose words are in the corpus. jmolt reads profiles before it replies, so a
    corpus without them turns a considered reply into a failed tool call."""
    names: list[str] = []
    for item in list(corpus.posts.values()) + [c for cs in corpus.comments.values() for c in cs]:
        name = _author(item)
        if name and name != corpus.handle and name not in names:
            names.append(name)
    return names
