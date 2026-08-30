"""A Moltbook the agent believes in, that does not exist.

`SimMoltbookClient` is the seam the jmolt simulator hangs off
(docs/plans/JMOLT_LEDGER_ENGINE_PLAN.md, S1). Every read and write jmolt makes already
funnels through `MoltbookClient._request`, so this subclasses it and overrides that ONE
method: reads are answered from a recorded corpus, writes are believed. Everything else —
the list caps and body truncation (M12), `_clean_sort`, `_page_limit`, the id/slug
sanitation, the rate ledger, the response shaping the tools render — is the production code
path, unchanged. Reimplementing twelve methods instead would have let the simulator's surface
drift from the real one, which is the one failure mode that would make every measurement
taken against it worthless.

Reaching the real platform is impossible rather than forbidden: the constructor takes no key
and installs a key provider that raises and a transport that raises, so a future code path
that somehow bypassed the override would still fail loudly rather than quietly posting to
Moltbook under jmolt's live credential.

**Writes are believed, and become visible to subsequent reads in the same sim night.** That
is not a convenience — an agent re-reading a thread and finding its own fresh comment is
exactly the condition that produced the self-reply failure, and a simulator that hides it
cannot reproduce the bug we built it to study.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from jbrain.web.moltbook import MoltbookClient, MoltbookError

# Synthetic ids are prefixed so anything that leaks out of a sim run — a scratchpad note, a
# ledger target, a scored transcript — is recognisable on sight as never having existed.
SIM_ID_PREFIX = "sim_"


class _NoKey:
    """A key provider that cannot yield a key. The simulator holds no credential; if
    something reaches for one, that is a bug worth a loud failure, not a silent fallback."""

    async def __call__(self) -> tuple[str, str]:
        raise MoltbookError("the simulator holds no Moltbook credential")


class _NoTransport(httpx.AsyncBaseTransport):
    """A transport that refuses to carry anything. `_request` is overridden below, so this
    is never reached in practice — which is the point: it is what makes 'the simulator cannot
    reach Moltbook' a property of the object rather than a property of the override."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise MoltbookError(f"the simulator has no network: refusing {request.method}")


@dataclass
class SimWrite:
    """One believed write, in the order jmolt made it."""

    seq: int
    kind: str  # post | comment | vote | follow | subscribe | profile
    at: datetime
    payload: dict[str, Any]
    sim_id: str = ""


@dataclass
class SimCorpus:
    """A recorded slice of Moltbook, as the platform's own JSON.

    Shapes mirror the live API exactly, because the production renderers read them: a post
    carries `id`/`title`/`content`/`author`/`created_at`/`score`/`submolt`, a comment carries
    `id`/`content`/`author`/`created_at` and an optional `replies` list. `feed` maps a sort
    name to an ordered list of post ids, so the same corpus can answer `hot` and `new`
    differently without storing the posts twice.
    """

    handle: str = "jmolt"
    captured_at: str = ""
    home: dict[str, Any] = field(default_factory=dict)
    me: dict[str, Any] = field(default_factory=dict)
    submolts: dict[str, Any] = field(default_factory=dict)
    posts: dict[str, dict[str, Any]] = field(default_factory=dict)
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    feed: dict[str, list[str]] = field(default_factory=dict)
    submolt_feed: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str | dict[str, Any]) -> SimCorpus:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_json(self) -> str:
        return json.dumps(
            {f: getattr(self, f) for f in self.__dataclass_fields__}, ensure_ascii=False
        )


class SimMoltbookClient(MoltbookClient):
    """Serves jmolt's reads from a corpus and believes its writes. Holds no credential and
    no working transport."""

    def __init__(
        self,
        corpus: SimCorpus,
        *,
        clock: Callable[[], datetime] | None = None,
        max_list_items: int | None = None,
        max_item_chars: int | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if max_list_items is not None:
            kwargs["max_list_items"] = max_list_items
        if max_item_chars is not None:
            kwargs["max_item_chars"] = max_item_chars
        super().__init__(
            _NoKey(),  # type: ignore[arg-type]
            base_url="sim://moltbook/api/v1",
            transport=_NoTransport(),
            **kwargs,
        )
        self._corpus = corpus
        self._clock = clock or (lambda: datetime.now(UTC))
        self.writes: list[SimWrite] = []
        # The read tools mark jmolt's OWN comments in a rendered thread off this. The real
        # client learns it from an authed call; the simulator has no authed call, so it is
        # seeded from the corpus and correct from the first read.
        self._last_handle = corpus.handle

    # ---- the one seam -----------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        authed: bool = True,
    ) -> Any:
        """Answer from the corpus instead of the network. Same contract as the real one: a
        dict of parsed JSON, or a `MoltbookError` for anything the platform would refuse."""
        params = params or {}
        if method == "GET":
            return self._read(path, params)
        return self._write_believed(method, path, json_body or {})

    # ---- reads ------------------------------------------------------------

    def _read(self, path: str, params: dict[str, Any]) -> Any:
        c = self._corpus
        if path == "/agents/status":
            return {"status": "claimed"}
        if path == "/home":
            return dict(c.home)
        if path == "/submolts":
            return dict(c.submolts)
        if path == "/agents/me":
            return self._me()
        if path == "/agents/profile":
            name = str(params.get("name", ""))
            profile = c.profiles.get(name)
            if profile is None:
                raise MoltbookError("no such agent", status=404)
            return dict(profile)
        if path == "/feed":
            return self._page(self._ordered(c.feed, str(params.get("sort", "hot"))), params)
        if path == "/posts":
            slug = str(params.get("submolt", ""))
            if slug not in c.submolt_feed:
                raise MoltbookError("no such submolt", status=404)
            return self._page([c.posts[p] for p in c.submolt_feed[slug] if p in c.posts], params)
        if path == "/search":
            return {"results": self._search(str(params.get("q", "")))}
        if path.startswith("/posts/") and path.endswith("/comments"):
            return self._comments(path.split("/")[2], params)
        if path.startswith("/posts/"):
            post = self._post(path.split("/")[2])
            if post is None:
                raise MoltbookError("no such post", status=404)
            return post
        raise MoltbookError(f"the corpus has no route for {path}", status=404)

    def _ordered(self, index: dict[str, list[str]], sort: str) -> list[dict[str, Any]]:
        """Posts in the recorded order for this sort, falling back to the corpus's first
        recorded ordering — a sort the harvest missed should not silently return nothing."""
        ids = index.get(sort) or next(iter(index.values()), [])
        return [self._corpus.posts[p] for p in ids if p in self._corpus.posts]

    def _page(self, items: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        """Cursor paging over a recorded list. The cursor is the offset, as a string — the
        tools only ever echo back what a previous page handed them."""
        limit = int(params.get("limit") or len(items))
        try:
            start = max(0, int(str(params.get("cursor") or 0)))
        except ValueError:
            start = 0
        window = items[start : start + limit]
        out: dict[str, Any] = {"posts": [dict(p) for p in window], "count": len(window)}
        if start + limit < len(items):
            out["has_more"] = True
            out["next_cursor"] = str(start + limit)
        return out

    def _post(self, post_id: str) -> dict[str, Any] | None:
        for w in self.writes:
            if w.kind == "post" and w.sim_id == post_id:
                return self._as_post(w)
        recorded = self._corpus.posts.get(post_id)
        return dict(recorded) if recorded else None

    def _comments(self, post_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._post(post_id) is None:
            raise MoltbookError("no such post", status=404)
        recorded = [dict(x) for x in self._corpus.comments.get(post_id, [])]
        mine = [self._as_comment(w) for w in self.writes if self._comment_on(w, post_id)]
        # Believed comments are NEWEST, which is what puts jmolt's own fresh reply at the top
        # of a `sort=new` re-read — the exact view that produced the self-reply.
        items = recorded + mine
        if str(params.get("sort", "new")) == "old":
            items = list(reversed(items))
        limit = int(params.get("limit") or len(items))
        return {"comments": items[:limit], "count": min(limit, len(items))}

    def _comment_on(self, w: SimWrite, post_id: str) -> bool:
        return w.kind == "comment" and str(w.payload.get("post_id", "")) == post_id

    def _search(self, query: str) -> list[dict[str, Any]]:
        """Substring match over recorded titles and bodies. Crude on purpose: the point is
        that `search` returns something plausible and bounded, not that it ranks well."""
        q = query.strip().lower()
        if not q:
            return []
        hits = [
            dict(p)
            for p in self._corpus.posts.values()
            if q in f"{p.get('title', '')} {p.get('content', '')}".lower()
        ]
        return hits

    def _me(self) -> dict[str, Any]:
        """The account, with tonight's believed posts prepended to `recentPosts` — this is
        what reconcile-before-publish (M23) and the tamper watch read."""
        me = dict(self._corpus.me)
        mine = [self._as_post(w) for w in self.writes if w.kind == "post"]
        recorded = me.get("recentPosts") or me.get("recent_posts") or []
        older = [dict(p) for p in recorded if isinstance(p, dict)]
        me["recentPosts"] = list(reversed(mine)) + older
        return me

    # ---- writes: believed -------------------------------------------------

    def _write_believed(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if method == "POST" and path == "/posts":
            w = self._record("post", body)
            return {"post": self._as_post(w)}
        if method == "POST" and path.startswith("/posts/") and path.endswith("/comments"):
            w = self._record("comment", {**body, "post_id": path.split("/")[2]})
            return {"comment": self._as_comment(w)}
        if method == "POST" and path.count("/") == 3:
            # /posts/{id}/upvote, /comments/{id}/downvote
            _, kind, target, direction = path.split("/")
            if direction in ("upvote", "downvote"):
                self._record("vote", {"kind": kind, "target_id": target, "direction": direction})
                return {"ok": True}
        if path.startswith("/agents/") and path.endswith("/follow"):
            name = path.split("/")[2]
            self._record("follow", {"name": name, "on": method == "POST"})
            return {"ok": True}
        if path.startswith("/submolts/") and path.endswith("/subscribe"):
            name = path.split("/")[2]
            self._record("subscribe", {"name": name, "on": method == "POST"})
            return {"ok": True}
        if method == "PATCH" and path == "/agents/me":
            self._record("profile", body)
            return {"ok": True}
        if method == "POST" and path == "/verify":
            # The simulator always accepts the challenge answer. Verification is platform
            # machinery, not behaviour under study, and a fake rejection here would show up
            # in the scores as a behaviour change that never happened.
            return {"verified": True}
        raise MoltbookError(f"the simulator has no write route for {method} {path}", status=404)

    def _record(self, kind: str, payload: dict[str, Any]) -> SimWrite:
        w = SimWrite(seq=len(self.writes) + 1, kind=kind, at=self._clock(), payload=dict(payload))
        w.sim_id = f"{SIM_ID_PREFIX}{kind}_{w.seq}"
        self.writes.append(w)
        return w

    # ---- believed writes, rendered as the platform would return them ------

    def _as_post(self, w: SimWrite) -> dict[str, Any]:
        return {
            "id": w.sim_id,
            "title": w.payload.get("title", ""),
            "content": w.payload.get("content", ""),
            "author": {"name": self._corpus.handle},
            "created_at": w.at.isoformat(),
            "score": 1,
            "submolt": {"name": w.payload.get("submolt_name", "")},
        }

    def _as_comment(self, w: SimWrite) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": w.sim_id,
            "content": w.payload.get("content", ""),
            "author": {"name": self._corpus.handle},
            "created_at": w.at.isoformat(),
            "score": 1,
        }
        if w.payload.get("parent_id"):
            item["parent_id"] = w.payload["parent_id"]
        return item
