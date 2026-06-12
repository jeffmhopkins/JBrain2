"""Tier-A memory: the pure ACE bullet-delta logic and the recall/edit/remember
service over a fake repo + fake embedder (no DB; RLS + SQL are integration)."""

import pytest

from jbrain.agent.memory import (
    EpisodeHit,
    MemoryBlock,
    MemoryService,
    apply_bullet_delta,
    parse_bullets,
    render_bullets,
)
from jbrain.db.session import SessionContext

CTX = SessionContext(principal_kind="owner")


class TestBulletDeltas:
    def test_parse_and_render_round_trip(self) -> None:
        body = "- prefers raw numbers\n- terse summaries"
        assert parse_bullets(body) == ["prefers raw numbers", "terse summaries"]
        assert render_bullets(parse_bullets(body)) == body

    def test_add_appends_a_bullet(self) -> None:
        assert apply_bullet_delta("- a", "add", "b") == "- a\n- b"

    def test_update_replaces_one_bullet_only(self) -> None:
        assert apply_bullet_delta("- a\n- b", "update", "B", target=1) == "- a\n- B"

    def test_remove_drops_one_bullet(self) -> None:
        assert apply_bullet_delta("- a\n- b\n- c", "remove", target=1) == "- a\n- c"

    def test_out_of_range_target_raises(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            apply_bullet_delta("- a", "update", "x", target=5)

    def test_unknown_op_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown bullet op"):
            apply_bullet_delta("- a", "rewrite", "x")


class FakeEmbedder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.queries += texts
        return [[0.1] * 384 for _ in texts]


class FakeMemoryRepo:
    def __init__(
        self,
        dense: list[EpisodeHit] | None = None,
        fts: list[EpisodeHit] | None = None,
        blocks: list[MemoryBlock] | None = None,
    ) -> None:
        self._dense = dense or []
        self._fts = fts or []
        self._blocks = blocks or []
        self.touched: list[str] = []
        self.written: list[dict] = []
        self.superseded: list[tuple[str, str]] = []

    async def recall_dense(self, ctx: object, qvec: object, limit: int) -> list[EpisodeHit]:
        return self._dense[:limit]

    async def recall_fts(self, ctx: object, q: str, limit: int) -> list[EpisodeHit]:
        return self._fts[:limit]

    async def touch(self, ctx: object, ids: list[str]) -> None:
        self.touched = list(ids)

    async def live_blocks(self, ctx: object, block_kind: str | None = None) -> list[MemoryBlock]:
        return [b for b in self._blocks if block_kind is None or b.block_kind == block_kind]

    async def write_block(self, ctx: object, **kwargs: object) -> str:
        self.written.append(kwargs)
        return "new-block"

    async def supersede_block(self, ctx: object, block_id: str, new_body_md: str) -> str:
        self.superseded.append((block_id, new_body_md))
        return "rev2"

    async def append_episode(self, ctx: object, **kwargs: object) -> str:
        self.appended = kwargs
        return "ep-new"


def episode(eid: str, importance: float = 0.0) -> EpisodeHit:
    return EpisodeHit(id=eid, body=f"trace {eid}", domain_scopes=("health",), importance=importance)


def service(repo: FakeMemoryRepo) -> tuple[MemoryService, FakeEmbedder]:
    embedder = FakeEmbedder()
    return MemoryService(repo, embedder, "embed-v1"), embedder  # type: ignore[arg-type]


class TestRecall:
    async def test_fuses_dense_and_fts_and_touches_results(self) -> None:
        # B appears in both legs → ranks first; recalled episodes are touched.
        repo = FakeMemoryRepo(dense=[episode("A"), episode("B")], fts=[episode("B"), episode("C")])
        svc, embedder = service(repo)
        hits = await svc.recall(CTX, "what about labs", limit=3)
        assert embedder.queries == ["what about labs"]
        assert hits[0].id == "B"
        assert {h.id for h in hits} == {"A", "B", "C"}
        assert set(repo.touched) == {"A", "B", "C"}

    async def test_importance_breaks_ties_toward_the_more_important_episode(self) -> None:
        # Same single-leg rank, but C is more important → C outranks A.
        repo = FakeMemoryRepo(dense=[episode("A", 0.0)], fts=[episode("C", 5.0)])
        svc, _ = service(repo)
        hits = await svc.recall(CTX, "q", limit=2)
        assert hits[0].id == "C"


class TestReadEditRemember:
    async def test_read_passes_block_kind_through(self) -> None:
        block = MemoryBlock("b1", "core", "general", "- a", 1)
        repo = FakeMemoryRepo(blocks=[block])
        svc, _ = service(repo)
        assert await svc.read(CTX, "core") == [block]
        assert await svc.read(CTX, "task") == []

    async def test_edit_applies_a_delta_and_supersedes(self) -> None:
        block = MemoryBlock("b1", "self_semantic", "health", "- raw numbers\n- terse", 2)
        repo = FakeMemoryRepo(blocks=[block])
        svc, _ = service(repo)
        new_id = await svc.edit(CTX, "b1", "remove", target=0)
        assert new_id == "rev2"
        assert repo.superseded == [("b1", "- terse")]

    async def test_edit_rejects_an_unknown_block(self) -> None:
        svc, _ = service(FakeMemoryRepo(blocks=[]))
        with pytest.raises(ValueError, match="no live memory block"):
            await svc.edit(CTX, "ghost", "add", "x")

    async def test_remember_writes_an_owner_confirmed_block(self) -> None:
        repo = FakeMemoryRepo()
        svc, _ = service(repo)
        await svc.remember(CTX, principal_id="p1", domain="health", body_md="- prefers raw numbers")
        assert repo.written[0]["source"] == "owner_confirmed"
        assert repo.written[0]["block_kind"] == "self_semantic"
        assert repo.written[0]["domain"] == "health"


class TestRecordEpisode:
    async def test_stamps_session_scopes_and_embeds_the_trace(self) -> None:
        repo = FakeMemoryRepo()
        svc, embedder = service(repo)
        await svc.record_episode(
            CTX, body="Asked: labs?", session_scopes=["general", "health"], run_id="r1"
        )
        # Nothing domain-specific observed → the full session scope set (fail-closed #4).
        assert repo.appended["domain_scopes"] == ("general", "health")
        assert repo.appended["embedding"] is not None
        assert embedder.queries == ["Asked: labs?"]

    async def test_observed_domains_narrow_the_stamp(self) -> None:
        repo = FakeMemoryRepo()
        svc, _ = service(repo)
        await svc.record_episode(
            CTX, body="b", session_scopes=["general", "health"], touched=["health"]
        )
        assert repo.appended["domain_scopes"] == ("health",)
