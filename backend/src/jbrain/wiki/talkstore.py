"""Talk-board read/write assembly (Phase 6, Wave T1 — `docs/mocks/wiki-talk-b-topics.html`).

The owner-only editorial board for an article: `discussion` topics + the auto `build_log` topic,
with append-only signed posts (voices `owner`/`editor`/`builder`). Every query runs on the
principal's RLS-scoped session; the tables are owner-only (mirroring `wiki_articles`), and the
board is served only for an **active** article — lockstep with the reader, which 404s a merged or
archived article. Posts are ordered by `(created_at, id)`; a `build_log` post carries a derived
1-based `rev` (the mock's "rev N"). The store is the sole writer — the API endpoints are thin.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


class TalkArticleNotFound(Exception):
    """No active article with that id is in the caller's scope (→ 404)."""


class TalkTopicNotFound(Exception):
    """No such topic on that article (→ 404)."""


class TalkBuildLogReadonly(Exception):
    """The owner cannot post to / resolve the auto Build-log topic (→ 409)."""


class TalkEditorConflict(Exception):
    """An Editor turn was requested but `after_post_id` is no longer the topic's latest post — a
    double-tap, a retry, or a reply-to-the-Editor's-own-reply (→ 409). The first turn moved the
    latest-post pointer, so the second request fails closed (no duplicate turn / correction)."""


def _iso(moment: datetime) -> str:
    return moment.isoformat()


class WikiTalkStore:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def get_board(self, ctx: SessionContext, article_id: str) -> dict[str, Any] | None:
        """The full board for an active article, or None (→ 404) when no active article is in
        scope. Discussion topics first (most-recent activity first), the Build log last."""
        try:
            aid = str(uuid.UUID(article_id))
        except ValueError:
            return None
        async with scoped_session(self._maker, ctx) as session:
            art = (
                await session.execute(
                    text("SELECT title FROM app.wiki_articles WHERE id = :a AND status = 'active'"),
                    {"a": aid},
                )
            ).first()
            if art is None:
                return None
            topics = (
                await session.execute(
                    text(
                        "SELECT id, kind, title, status, last_post_at"
                        " FROM app.wiki_talk_topics WHERE article_id = :a"
                        # discussion before build_log; within discussions, newest activity first.
                        " ORDER BY (kind = 'build_log'), last_post_at DESC"
                    ),
                    {"a": aid},
                )
            ).all()
            out_topics: list[dict[str, Any]] = []
            for t in topics:
                posts = (
                    await session.execute(
                        text(
                            "SELECT id, author, body, source_json, outcome, created_at"
                            " FROM app.wiki_talk_posts WHERE topic_id = :t"
                            " ORDER BY created_at, id"
                        ),
                        {"t": t.id},
                    )
                ).all()
                is_log = t.kind == "build_log"
                out_topics.append(
                    {
                        "id": str(t.id),
                        "kind": t.kind,
                        "title": t.title,
                        "status": t.status,
                        "meta": f"auto · {len(posts)} entries" if is_log else None,
                        "posts": [
                            {
                                "id": str(p.id),
                                "author": p.author,
                                "body": p.body,
                                "source": p.source_json,
                                "outcome": p.outcome,
                                "created_at": _iso(p.created_at),
                                # The Build-log "rev N" is derived from 1-based post order.
                                "rev": (i + 1) if is_log else None,
                            }
                            for i, p in enumerate(posts)
                        ],
                    }
                )
        return {"title": art.title, "topics": out_topics}

    async def create_topic(
        self, ctx: SessionContext, article_id: str, *, title: str, body: str
    ) -> dict[str, Any]:
        """Open a discussion topic + its first owner post atomically. Raises TalkArticleNotFound
        when no active article is in scope (checked in the same scoped session as the write)."""
        aid = self._as_uuid(article_id)
        async with scoped_session(self._maker, ctx) as session:
            await self._require_active_article(session, aid)
            topic_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.wiki_talk_topics (article_id, kind, title, status)"
                        " VALUES (:a, 'discussion', :t, 'open') RETURNING id"
                    ),
                    {"a": aid, "t": title},
                )
            ).scalar()
            post = await self._insert_owner_post(session, topic_id, body)
        return {
            "id": str(topic_id),
            "kind": "discussion",
            "title": title,
            "status": "open",
            "meta": None,
            "posts": [post],
        }

    async def add_reply(
        self, ctx: SessionContext, article_id: str, topic_id: str, *, body: str
    ) -> dict[str, Any]:
        """Append an owner reply to a discussion topic. Raises TalkBuildLogReadonly for the
        Build-log topic (409) and TalkTopicNotFound when the topic isn't on that active article."""
        aid, tid = self._as_uuid(article_id), self._as_uuid(topic_id)
        async with scoped_session(self._maker, ctx) as session:
            await self._require_active_article(session, aid)
            topic = (
                await session.execute(
                    text("SELECT kind FROM app.wiki_talk_topics WHERE id = :t AND article_id = :a"),
                    {"t": tid, "a": aid},
                )
            ).first()
            if topic is None:
                raise TalkTopicNotFound
            if topic.kind == "build_log":
                raise TalkBuildLogReadonly
            post = await self._insert_owner_post(session, tid, body)
            await session.execute(
                text("UPDATE app.wiki_talk_topics SET last_post_at = now() WHERE id = :t"),
                {"t": tid},
            )
        return post

    async def set_status(
        self, ctx: SessionContext, article_id: str, topic_id: str, *, status: str
    ) -> dict[str, Any]:
        """Resolve/reopen a discussion topic. 409 on the Build-log; 404 otherwise."""
        aid, tid = self._as_uuid(article_id), self._as_uuid(topic_id)
        async with scoped_session(self._maker, ctx) as session:
            await self._require_active_article(session, aid)
            topic = (
                await session.execute(
                    text("SELECT kind FROM app.wiki_talk_topics WHERE id = :t AND article_id = :a"),
                    {"t": tid, "a": aid},
                )
            ).first()
            if topic is None:
                raise TalkTopicNotFound
            if topic.kind == "build_log":
                raise TalkBuildLogReadonly
            await session.execute(
                text("UPDATE app.wiki_talk_topics SET status = :s WHERE id = :t"),
                {"s": status, "t": tid},
            )
        return {"id": str(tid), "status": status}

    async def topic_for_editor(
        self, ctx: SessionContext, article_id: str, topic_id: str, after_post_id: str
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Load `(topic_title, article_title, posts)` for an Editor turn, in one scoped session:
        active-article guard (404), discussion-kind guard (409 on build_log), and the idempotency
        guard — `after_post_id` MUST be the topic's latest post (else TalkEditorConflict, 409),
        which also requires the latest to be the owner reply just filed."""
        aid, tid = self._as_uuid(article_id), self._as_uuid(topic_id)
        async with scoped_session(self._maker, ctx) as session:
            art = (
                await session.execute(
                    text("SELECT title FROM app.wiki_articles WHERE id = :a AND status = 'active'"),
                    {"a": aid},
                )
            ).first()
            if art is None:
                raise TalkArticleNotFound
            topic = (
                await session.execute(
                    text(
                        "SELECT kind, title FROM app.wiki_talk_topics"
                        " WHERE id = :t AND article_id = :a"
                    ),
                    {"t": tid, "a": aid},
                )
            ).first()
            if topic is None:
                raise TalkTopicNotFound
            if topic.kind == "build_log":
                raise TalkBuildLogReadonly
            posts = (
                await session.execute(
                    text(
                        "SELECT id, author, body, source_json, outcome, created_at"
                        " FROM app.wiki_talk_posts WHERE topic_id = :t ORDER BY created_at, id"
                    ),
                    {"t": tid},
                )
            ).all()
            if not posts or str(posts[-1].id) != self._as_uuid(after_post_id):
                raise TalkEditorConflict
            mapped = [
                {
                    "id": str(p.id),
                    "author": p.author,
                    "body": p.body,
                    "source": p.source_json,
                    "outcome": p.outcome,
                    "created_at": _iso(p.created_at),
                }
                for p in posts
            ]
        return topic.title, str(art.title), mapped

    async def add_editor_post(
        self, ctx: SessionContext, article_id: str, topic_id: str, *, body: str, outcome: str | None
    ) -> dict[str, Any]:
        """Append the Editor's post (author='editor', optional outcome chip). Re-checks the active-
        article + discussion-kind invariants in the SAME session as the insert (no TOCTOU vs the
        earlier `topic_for_editor` load), and bumps `last_post_at`."""
        aid, tid = self._as_uuid(article_id), self._as_uuid(topic_id)
        async with scoped_session(self._maker, ctx) as session:
            await self._require_active_article(session, aid)
            topic = (
                await session.execute(
                    text("SELECT kind FROM app.wiki_talk_topics WHERE id = :t AND article_id = :a"),
                    {"t": tid, "a": aid},
                )
            ).first()
            if topic is None:
                raise TalkTopicNotFound
            if topic.kind == "build_log":
                raise TalkBuildLogReadonly
            row = (
                await session.execute(
                    text(
                        "INSERT INTO app.wiki_talk_posts (topic_id, author, body, outcome)"
                        " VALUES (:t, 'editor', :b, :o) RETURNING id, created_at"
                    ),
                    {"t": tid, "b": body, "o": outcome},
                )
            ).first()
            assert row is not None  # INSERT ... RETURNING always yields the row
            await session.execute(
                text("UPDATE app.wiki_talk_topics SET last_post_at = now() WHERE id = :t"),
                {"t": tid},
            )
        return {
            "id": str(row.id),
            "author": "editor",
            "body": body,
            "source": None,
            "outcome": outcome,
            "created_at": _iso(row.created_at),
            "rev": None,
        }

    # ---- helpers -------------------------------------------------------------------------

    @staticmethod
    def _as_uuid(value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise TalkArticleNotFound from exc

    @staticmethod
    async def _require_active_article(session: AsyncSession, aid: str) -> None:
        seen = (
            await session.execute(
                text("SELECT 1 FROM app.wiki_articles WHERE id = :a AND status = 'active'"),
                {"a": aid},
            )
        ).first()
        if seen is None:
            raise TalkArticleNotFound

    @staticmethod
    async def _insert_owner_post(session: AsyncSession, topic_id: Any, body: str) -> dict[str, Any]:
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.wiki_talk_posts (topic_id, author, body)"
                    " VALUES (:t, 'owner', :b) RETURNING id, created_at"
                ),
                {"t": topic_id, "b": body},
            )
        ).first()
        assert row is not None  # INSERT ... RETURNING always yields the row
        return {
            "id": str(row.id),
            "author": "owner",
            "body": body,
            "source": None,
            "outcome": None,
            "created_at": _iso(row.created_at),
            "rev": None,
        }
