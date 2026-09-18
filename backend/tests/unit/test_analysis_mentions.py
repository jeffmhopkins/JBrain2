"""Unit tests for the incremental mention seam (analysis/pipeline.py).

`_upsert_mentions` (in `commit_facts`) + `_reconcile_mentions` (in `settle_note`,
over the union of every pass's ids) replace a wipe-and-reinsert that was only ever
correct for a single whole-note pass: called twice, the second pass's DELETE
removed everything the first wrote. The matching rule is the whole point, so it is
tested away from Postgres — the pass keeps a row whose (chunk, span, entity) it
re-asserts, inserts only genuinely new anchors, and reconcile removes exactly what
was not asserted. Whole-note end state, the shared-span multiset and id stability
across a re-analysis are pinned in `test_apply_intent_pg.py`.
"""

import struct
import uuid
from typing import Any, cast

import pytest

from jbrain.analysis.entities import ResolvedEntity
from jbrain.analysis.extraction import ExtractedMention, Extraction
from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef
from jbrain.analysis.settle_owner import ANALYZER
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.models import EntityMention

_NOTE = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CHUNK = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ENTITY = uuid.UUID("33333333-3333-3333-3333-333333333333")
_OTHER = uuid.UUID("44444444-4444-4444-4444-444444444444")


class _Result:
    def __init__(self, rows: list[EntityMention]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[EntityMention]:
        return self._rows


class _StubSession:
    """The two operations the mention seam uses: one SELECT of the note's rows,
    and `add` for a row it has to mint."""

    def __init__(self, rows: list[EntityMention] | None = None) -> None:
        self._rows = rows or []
        self.added: list[EntityMention] = []
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.executed.append(stmt)
        return _Result(self._rows)

    def add(self, obj: EntityMention) -> None:
        self.added.append(obj)


def _hex(value: uuid.UUID) -> str:
    """How a bound UUID renders inside the compiled statement."""
    return value.hex


def _pipeline() -> AnalysisPipeline:
    # The mention seam touches neither the maker nor the router.
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    return AnalysisPipeline(cast(Any, None), router)


def _extraction(*mentions: tuple[str, str]) -> Extraction:
    return Extraction(
        title="t",
        tags=[],
        mentions=[ExtractedMention(name=n, kind="Person", surface_text=s) for n, s in mentions],
        facts=[],
        tokens=[],
    )


def _row(
    *,
    entity_id: uuid.UUID = _ENTITY,
    start: int = 0,
    end: int = 4,
    surface: str = "Cleo",
    link_method: str = "exact_alias",
    confidence: float | None = 1.0,
) -> EntityMention:
    return EntityMention(
        id=uuid.uuid4(),
        entity_id=entity_id,
        chunk_id=_CHUNK,
        note_id=_NOTE,
        surface_text=surface,
        char_start=start,
        char_end=end,
        link_method=link_method,
        confidence=confidence,
        domain_code="general",
        settle_owners=[ANALYZER],
    )


async def _upsert(
    session: _StubSession, extraction: Extraction, resolved: dict[str, ResolvedEntity | None]
) -> tuple[dict[str, tuple[uuid.UUID, int, int]], set[uuid.UUID]]:
    return await _pipeline()._upsert_mentions(
        cast(Any, session),
        extraction,
        resolved,
        _NOTE,
        "general",
        [_ChunkRef(id=_CHUNK, text="Cleo and Ada were here.")],
        ANALYZER,
    )


@pytest.mark.asyncio
async def test_reasserted_mention_keeps_its_row() -> None:
    existing = _row()
    session = _StubSession([existing])

    _anchors, asserted = await _upsert(
        session,
        _extraction(("Cleo", "Cleo")),
        {"Cleo": ResolvedEntity(id=_ENTITY, subject_id=None)},
    )

    # The id survives — a re-run must not churn ids the wiki dirty-bit trigger
    # and the co-mention spine are built on.
    assert asserted == {existing.id}
    assert session.added == []


@pytest.mark.asyncio
async def test_new_anchor_inserts_and_stale_row_is_not_asserted() -> None:
    stale = _row(start=9, end=12, surface="Ada", entity_id=_OTHER)
    session = _StubSession([stale])

    _anchors, asserted = await _upsert(
        session,
        _extraction(("Cleo", "Cleo")),
        {"Cleo": ResolvedEntity(id=_ENTITY, subject_id=None)},
    )

    assert len(session.added) == 1
    assert asserted == {session.added[0].id}
    assert stale.id not in asserted  # reconcile is what removes it


@pytest.mark.asyncio
async def test_two_mentions_on_one_span_keep_two_rows() -> None:
    """(chunk, span, entity) is not unique: two mention entries can anchor to the
    same surface for the same entity, and the wipe-and-reinsert wrote two rows.
    Matching pops, so the multiset is preserved rather than collapsed."""
    first, second = _row(), _row()
    session = _StubSession([first, second])

    _anchors, asserted = await _upsert(
        session,
        _extraction(("Cleo", "Cleo"), ("Cleo the cat", "Cleo")),
        {
            "Cleo": ResolvedEntity(id=_ENTITY, subject_id=None),
            "Cleo the cat": ResolvedEntity(id=_ENTITY, subject_id=None),
        },
    )

    assert asserted == {first.id, second.id}
    assert session.added == []


@pytest.mark.asyncio
async def test_changed_link_method_refreshes_the_kept_row() -> None:
    existing = _row(link_method="exact_alias", confidence=1.0)
    session = _StubSession([existing])

    await _upsert(
        session,
        _extraction(("Cleo", "Cleo")),
        {"Cleo": ResolvedEntity(id=_ENTITY, subject_id=None, method="llm", confidence=0.7)},
    )

    assert (existing.link_method, existing.confidence) == ("llm", 0.7)


@pytest.mark.asyncio
async def test_float4_round_tripped_confidence_is_not_a_change() -> None:
    """`entity_mentions.confidence` is Postgres `real`, so a resolver's 0.9 reads
    back as 0.8999999761581421 — only 1.0 round-trips exactly. Compared exactly,
    every embedding- and relationship-linked mention would count as "changed" on
    every re-run, UPDATE, and re-dirty its article through 0046's trigger."""
    stored = struct.unpack("f", struct.pack("f", 0.9))[0]
    assert stored != 0.9  # the round trip really is lossy
    existing = _row(confidence=stored)
    session = _StubSession([existing])

    await _upsert(
        session,
        _extraction(("Cleo", "Cleo")),
        {"Cleo": ResolvedEntity(id=_ENTITY, subject_id=None, method="llm", confidence=0.9)},
    )

    assert existing.confidence == stored  # untouched — no UPDATE, no re-dirty


@pytest.mark.asyncio
async def test_a_real_confidence_change_still_writes() -> None:
    existing = _row(confidence=0.9)
    session = _StubSession([existing])

    await _upsert(
        session,
        _extraction(("Cleo", "Cleo")),
        {"Cleo": ResolvedEntity(id=_ENTITY, subject_id=None, confidence=0.55)},
    )

    assert existing.confidence == 0.55


@pytest.mark.asyncio
async def test_relationship_method_still_maps_to_exact_alias() -> None:
    """0006 CHECKs link_method; layer 2b's "relationship" is wider than the enum."""
    session = _StubSession()

    await _upsert(
        session,
        _extraction(("Cleo", "Cleo")),
        {"Cleo": ResolvedEntity(id=_ENTITY, subject_id=None, method="relationship")},
    )

    assert session.added[0].link_method == "exact_alias"


@pytest.mark.asyncio
async def test_unresolved_or_unlocatable_mention_writes_nothing() -> None:
    session = _StubSession()

    anchors, asserted = await _upsert(
        session,
        _extraction(("Cleo", "Cleo"), ("Nobody", "not in the text")),
        {"Cleo": None},
    )

    assert (session.added, asserted) == ([], set())
    # An unresolved name still anchors: fact provenance and the <mark>ed citation
    # come from the span, not the link — and a paraphrase keeps its zero-width
    # anchor rather than being dropped (merges stay reversible).
    assert anchors["Cleo"] == (_CHUNK, 0, 4)
    assert anchors["Nobody"] == (_CHUNK, 0, 0)


@pytest.mark.asyncio
async def test_reconcile_releases_this_producers_claim_and_spares_the_asserted_ids() -> None:
    """The reconcile no longer deletes on sight: it RELEASES this producer's claim on
    the note's rows it did not assert, and only what is left unclaimed goes."""
    session = _StubSession()
    kept = uuid.uuid4()

    await _pipeline()._reconcile_mentions(cast(Any, session), _NOTE, {kept}, ANALYZER)

    sql = str(session.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "UPDATE app.entity_mentions" in sql
    assert "array_remove" in sql
    assert _hex(_NOTE) in sql and _hex(kept) in sql
    assert "NOT IN" in sql.upper()
    # Nothing came back unclaimed, so no DELETE was issued at all.
    assert len(session.executed) == 1


@pytest.mark.asyncio
async def test_reconcile_with_nothing_asserted_releases_every_row_it_claims() -> None:
    session = _StubSession()

    await _pipeline()._reconcile_mentions(cast(Any, session), _NOTE, set(), ANALYZER)

    sql = str(session.executed[0].compile(compile_kwargs={"literal_binds": True}))
    assert "NOT IN" not in sql.upper()
    assert _hex(_NOTE) in sql


@pytest.mark.asyncio
async def test_reconcile_deletes_only_the_rows_no_producer_still_claims() -> None:
    """The half the claim set exists for: a span the CONVERSATION also anchors survives
    the analyzer letting go, and only the row whose set emptied is deleted."""
    orphaned = _row()
    orphaned.settle_owners = []  # what the release's RETURNING hands back
    shared = _row(entity_id=_OTHER)
    shared.settle_owners = ["conversation"]
    session = _StubSession([orphaned, shared])

    await _pipeline()._reconcile_mentions(cast(Any, session), _NOTE, set(), ANALYZER)

    assert len(session.executed) == 2
    sql = str(session.executed[1].compile(compile_kwargs={"literal_binds": True}))
    assert "DELETE FROM app.entity_mentions" in sql
    assert _hex(orphaned.id) in sql
    assert _hex(shared.id) not in sql
