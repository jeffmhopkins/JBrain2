"""Tier-A memory service: recall over the episodic namespace and ACE delta-edit
writes to the behavioral/working blocks (docs/ASSISTANT.md "Memory model").

Two distinct jobs, one table family. *Recall* fuses the same dense+FTS RRF the
corpus uses, but over `agent_episodes` — a segregated namespace (its own table),
so an episodic trace can never be matched as a citable chunk; recall returns
*data*, never instruction (invariant #3). *Blocks* are MD-as-rows edited the ACE
way — ADD/UPDATE/REMOVE on individual bullets, never full rewrites, since full
regeneration rots accumulated self-knowledge — and every edit appends a new
revision rather than mutating in place.

RLS is the firewall: every query runs on the caller's scoped session, so a
narrowed session only ever recalls episodes it holds all the scopes for, and the
classifier (agent/classifier.py) decides the stamp at write time.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.classifier import episodic_scopes
from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import EmbedClient, vector_literal
from jbrain.search.service import RRF_K, rrf_scores

# --- ACE bullet deltas (pure) ----------------------------------------------

BulletOp = str  # "add" | "update" | "remove"


def parse_bullets(body_md: str) -> list[str]:
    """The bullets of an MD memory block — lines starting with `- `."""
    return [
        line.strip()[2:].strip() for line in body_md.splitlines() if line.strip().startswith("- ")
    ]


def render_bullets(bullets: Sequence[str]) -> str:
    return "\n".join(f"- {b}" for b in bullets)


def apply_bullet_delta(
    body_md: str, op: BulletOp, text: str = "", target: int | None = None
) -> str:
    """Apply one ACE delta to a block body and return the new body. ADD appends a
    bullet; UPDATE/REMOVE act on the 0-based `target` bullet. Never a full rewrite
    — the op touches exactly one bullet, so accumulated self-knowledge is
    preserved (ACE). An out-of-range target or unknown op raises."""
    bullets = parse_bullets(body_md)
    if op == "add":
        bullets.append(text.strip())
    elif op in ("update", "remove"):
        if target is None or not 0 <= target < len(bullets):
            raise ValueError(f"{op}: bullet index {target} out of range")
        if op == "update":
            bullets[target] = text.strip()
        else:
            del bullets[target]
    else:
        raise ValueError(f"unknown bullet op: {op!r}")
    return render_bullets(bullets)


# --- Rows ------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryBlock:
    id: str
    block_kind: str
    domain: str
    body_md: str
    revision: int


@dataclass(frozen=True)
class EpisodeHit:
    id: str
    body: str
    domain_scopes: tuple[str, ...]
    importance: float


# --- SQL repo --------------------------------------------------------------

_RECALL_SELECT = "SELECT id, body, domain_scopes, importance FROM app.agent_episodes"
_DENSE = (
    _RECALL_SELECT
    + " WHERE embedding IS NOT NULL ORDER BY embedding <=> cast(:qvec AS vector), id LIMIT :limit"
)
_FTS = (
    _RECALL_SELECT
    + " WHERE tsv @@ websearch_to_tsquery('english', :q)"
    + " ORDER BY ts_rank(tsv, websearch_to_tsquery('english', :q)) DESC, id LIMIT :limit"
)


def _episode(row: object) -> EpisodeHit:
    return EpisodeHit(
        id=str(row.id),  # type: ignore[attr-defined]
        body=row.body,  # type: ignore[attr-defined]
        domain_scopes=tuple(row.domain_scopes),  # type: ignore[attr-defined]
        importance=float(row.importance),  # type: ignore[attr-defined]
    )


class MemoryRepo:
    """CRUD for agent memory on RLS-scoped sessions. The RLS policies (migration
    0017) are the firewall — this repo never re-checks scope."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def live_blocks(
        self, ctx: SessionContext, block_kind: str | None = None
    ) -> list[MemoryBlock]:
        sql = (
            "SELECT id, block_kind, domain_code, body_md, revision FROM app.agent_memory"
            " WHERE superseded_by IS NULL"
            + (" AND block_kind = :kind" if block_kind else "")
            + " ORDER BY block_kind, domain_code"
        )
        params = {"kind": block_kind} if block_kind else {}
        async with scoped_session(self._maker, ctx) as session:
            rows = (await session.execute(text(sql), params)).all()
        return [
            MemoryBlock(str(r.id), r.block_kind, r.domain_code, r.body_md, r.revision) for r in rows
        ]

    async def write_block(
        self,
        ctx: SessionContext,
        *,
        principal_id: str,
        domain: str,
        block_kind: str,
        body_md: str,
        subject_id: str | None = None,
        source: str = "owner_confirmed",
        revision: int = 1,
    ) -> str:
        async with scoped_session(self._maker, ctx) as session:
            row_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.agent_memory"
                        " (principal_id, subject_id, domain_code, block_kind, body_md,"
                        "  revision, source)"
                        " VALUES (:pid, :sid, :domain, :kind, :body, :rev, :source) RETURNING id"
                    ),
                    {
                        "pid": principal_id,
                        "sid": subject_id,
                        "domain": domain,
                        "kind": block_kind,
                        "body": body_md,
                        "rev": revision,
                        "source": source,
                    },
                )
            ).scalar()
        return str(row_id)

    async def supersede_block(self, ctx: SessionContext, block_id: str, new_body_md: str) -> str:
        """Write the edited block as a new revision and point the old one at it —
        append-only history, never an in-place mutation."""
        async with scoped_session(self._maker, ctx) as session:
            old = (
                await session.execute(
                    text(
                        "SELECT principal_id, subject_id, domain_code, block_kind, revision, source"
                        " FROM app.agent_memory WHERE id = :id AND superseded_by IS NULL"
                    ),
                    {"id": block_id},
                )
            ).one_or_none()
            if old is None:
                raise ValueError("no live memory block with that id")
            new_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.agent_memory"
                        " (principal_id, subject_id, domain_code, block_kind, body_md,"
                        "  revision, source)"
                        " VALUES (:pid, :sid, :domain, :kind, :body, :rev, :source) RETURNING id"
                    ),
                    {
                        "pid": str(old.principal_id),
                        "sid": str(old.subject_id) if old.subject_id else None,
                        "domain": old.domain_code,
                        "kind": old.block_kind,
                        "body": new_body_md,
                        "rev": old.revision + 1,
                        "source": old.source,
                    },
                )
            ).scalar()
            await session.execute(
                text("UPDATE app.agent_memory SET superseded_by = :new WHERE id = :old"),
                {"new": str(new_id), "old": block_id},
            )
        return str(new_id)

    async def recall_dense(
        self, ctx: SessionContext, qvec: list[float], limit: int
    ) -> list[EpisodeHit]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(text(_DENSE), {"qvec": vector_literal(qvec), "limit": limit})
            ).all()
        return [_episode(r) for r in rows]

    async def recall_fts(self, ctx: SessionContext, q: str, limit: int) -> list[EpisodeHit]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (await session.execute(text(_FTS), {"q": q, "limit": limit})).all()
        return [_episode(r) for r in rows]

    async def touch(self, ctx: SessionContext, ids: Sequence[str]) -> None:
        if not ids:
            return
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "UPDATE app.agent_episodes SET last_accessed_at = now()"
                    " WHERE id = ANY(cast(:ids AS uuid[]))"
                ),
                {"ids": list(ids)},
            )

    async def append_episode(
        self,
        ctx: SessionContext,
        *,
        body: str,
        domain_scopes: Sequence[str],
        embedding: list[float] | None,
        embedding_model: str | None,
        session_id: str | None = None,
        run_id: str | None = None,
        importance: float = 0.0,
    ) -> str:
        async with scoped_session(self._maker, ctx) as session:
            row_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.agent_episodes"
                        " (session_id, run_id, domain_scopes, body, importance, embedding,"
                        "  embedding_model)"
                        " VALUES (:sid, :rid, :scopes, :body, :imp,"
                        "  cast(:emb AS vector), :model) RETURNING id"
                    ),
                    {
                        "sid": session_id,
                        "rid": run_id,
                        "scopes": list(domain_scopes),
                        "body": body,
                        "imp": importance,
                        "emb": vector_literal(embedding) if embedding is not None else None,
                        "model": embedding_model,
                    },
                )
            ).scalar()
        return str(row_id)


# --- Service ---------------------------------------------------------------

# How much an episode's importance nudges its fused recall rank. Small: recall is
# similarity-first, importance is a tiebreak (owner-confirmed signals raise it).
_IMPORTANCE_WEIGHT = 1.0 / RRF_K


class MemoryService:
    """Recall + behavioral read/write over MemoryRepo, embedding queries through
    the adapter's embed client. Recall is similarity-first (dense+FTS RRF) with a
    light importance tiebreak; recalled episodes are touched so recency tracks
    use."""

    def __init__(self, repo: MemoryRepo, embedder: EmbedClient, embedding_model: str):
        self._repo = repo
        self._embedder = embedder
        self._model = embedding_model

    async def recall(self, ctx: SessionContext, query: str, limit: int = 5) -> list[EpisodeHit]:
        qvec = (await self._embedder.embed([query]))[0]
        pool = max(limit * 2, limit)
        dense = await self._repo.recall_dense(ctx, qvec, pool)
        fts = await self._repo.recall_fts(ctx, query, pool)
        scores = rrf_scores([h.id for h in dense], [h.id for h in fts])
        by_id = {h.id: h for h in [*dense, *fts]}
        for hid, hit in by_id.items():
            scores[hid] = scores.get(hid, 0.0) + hit.importance * _IMPORTANCE_WEIGHT
        ranked = sorted(by_id.values(), key=lambda h: scores[h.id], reverse=True)[:limit]
        await self._repo.touch(ctx, [h.id for h in ranked])
        return ranked

    async def read(self, ctx: SessionContext, block_kind: str | None = None) -> list[MemoryBlock]:
        return await self._repo.live_blocks(ctx, block_kind)

    async def record_episode(
        self,
        ctx: SessionContext,
        *,
        body: str,
        session_scopes: Sequence[str],
        touched: Sequence[str] = (),
        session_id: str | None = None,
        run_id: str | None = None,
        importance: float = 0.0,
    ) -> str:
        """Auto-append an episodic trace for a finished turn. The classifier stamps
        it fail-closed (every scope touched, bounded by the session; the full
        session scope when nothing domain-specific was observed), so a later
        session can recall it only if it holds all those scopes (#4)."""
        scopes = episodic_scopes(touched, session_scopes)
        vec = (await self._embedder.embed([body]))[0]
        return await self._repo.append_episode(
            ctx,
            body=body,
            domain_scopes=scopes,
            embedding=vec,
            embedding_model=self._model,
            session_id=session_id,
            run_id=run_id,
            importance=importance,
        )

    async def remember(
        self,
        ctx: SessionContext,
        *,
        principal_id: str,
        domain: str,
        body_md: str,
        block_kind: str = "self_semantic",
        subject_id: str | None = None,
    ) -> str:
        """Create a behavioral/self-semantic block. Caller MUST have established
        owner confirmation (invariant #3) — this writes source='owner_confirmed'."""
        return await self._repo.write_block(
            ctx,
            principal_id=principal_id,
            domain=domain,
            block_kind=block_kind,
            body_md=body_md,
            subject_id=subject_id,
            source="owner_confirmed",
        )

    async def edit(
        self,
        ctx: SessionContext,
        block_id: str,
        op: BulletOp,
        text_: str = "",
        target: int | None = None,
    ) -> str:
        """Apply one ACE bullet delta and persist it as a new revision."""
        blocks = {b.id: b for b in await self._repo.live_blocks(ctx)}
        block = blocks.get(block_id)
        if block is None:
            raise ValueError("no live memory block with that id in scope")
        new_body = apply_bullet_delta(block.body_md, op, text_, target)
        return await self._repo.supersede_block(ctx, block_id, new_body)
