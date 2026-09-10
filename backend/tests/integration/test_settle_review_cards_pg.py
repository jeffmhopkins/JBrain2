"""Two producers, one note, one review inbox — can a settle delete a card it did not
file?

The settle's fact and mention halves were scoped to their producer by migration 0196;
its two REVIEW-CARD halves (`_sweep_stale_ambiguous`, `_sync_truncation_review`, both
in `analysis/pipeline.py`) were left note-keyed and producer-blind, and that residual is
what this file closes with `review_items.settle_owner` (migration 0197).

The failure was deterministic and owner-facing, not a race. A health `Records` note
carrying a decrypted EMR PDF fans `note.ingested` out to BOTH `integrate_note` (trigger
0040, no payload filter) and `emr_parse` (0122). The analyzer's extraction hits the
per-note fact cap on a long medical-history dump — the exact case
`_sync_truncation_review`'s own docstring names — and files the card that tells the
owner their tail was dropped. Then `EmrNoteCommit.settle()` runs, and EMR CANNOT
truncate (`ingest/emr/integrate.py`: the cap lives in `parse_extraction`, on the LLM
path, and `lower_parse_result` lowers every parsed encounter and orphan observation with
no cap anywhere), so it always took the `dropped <= 0` clear branch and deleted the
analyzer's card. The owner was never told.

`_sweep_stale_ambiguous` was quieter and worse in the same direction: its `NOT IN
:names` clause spared cards whose name the SETTLING producer still references, and an
EMR `Extraction`'s refs are semantic keys (`org:Quest`, `cond:E11.9`) sharing no surface
with anything the analyzer cards — so the clause excluded nothing and an EMR settle
deleted essentially every open `ambiguous_mention` card on the note.

Six tests. Three are the loss itself — an EMR settle over each card kind, and the mirror
direction where a truncating settle rewrote a card it did not file. Three keep the fix
honest, because a card nobody can retire is as much a defect as one anybody can delete:
the filer still retires its OWN stale cards, an EMR import files no truncation card
(it has nothing to report, which is the other half of this bug), and `status = 'open'`
still keeps a card the owner already answered out of every sweep.

The LLM is faked throughout (CLAUDE.md #5): the analyzer's half goes through
`apply_intent` over a pre-built intent, the EMR half parses a real Epic fixture
deterministically, and neither router is ever called out through.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

from jbrain.analysis.arbiter import plan_intent
from jbrain.analysis.display import truncation_display
from jbrain.analysis.extraction import ExtractedMention, Extraction
from jbrain.analysis.intent import AttestedSpan, EntityResolution
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.settle_owner import ANALYZER, EMR
from jbrain.db.session import scoped_session
from jbrain.ingest.emr.epic import parse_epic
from jbrain.ingest.emr.integrate import integrate_parse_result
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, EntityAlias, ReviewItem
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_apply_intent_pg import (
    _SURFACE,
    _fact,
    _intent,
    _load_chunks,
)
from tests.integration.test_extraction_pg import (  # noqa: F401
    ingest,
    make_note,
    maker,
)
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_EPIC = Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "epic_report.txt"

ANALYZER_EXTRACTOR = "xai:grok-4.3"
"""What `integrate_note` passes: `f"{provider}:{model}"` (`pipeline.py`). The card
sweeps never read it — the producer key is a separate stamp for the reason
`analysis/settle_owner.py` gives — but spelling it out keeps the two apart here."""


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    return AnalysisPipeline(maker, router)


async def _records_note(maker, tmp_path) -> str:  # noqa: F811
    """A health note both producers will settle — the shape of the live box's `Records`
    note, whose `note.ingested` reaches `integrate_note` and `emr_parse` alike."""
    note_id = await make_note(
        maker, domain="health", body="Imported EMR records. Long medical history follows."
    )
    await ingest(maker, note_id, tmp_path)
    return note_id


async def _analyzer_settle(
    maker,  # noqa: F811
    note_id: str,
    *,
    dropped_facts: int = 0,
    org: str | None = None,
) -> None:
    """The analyzer's commit+settle over one fact of its own — `apply_intent` is the
    highest seam that runs without a live model, i.e. exactly what `integrate_note`
    calls once the arbiter has ruled.

    `dropped_facts` is the per-note cap's tail-drop count riding out of
    `parse_extraction`; a non-zero one is what files the `extraction_truncated` card."""
    name = org or f"Globexit {uuid.uuid4().hex[:8]}"
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Organization", new_name=name)],
        [
            _fact(
                "m1",
                predicate="industry",
                statement=f"{name} is in tech",
                attested_span=AttestedSpan("c", name),
            )
        ],
    )
    chunks = await _load_chunks(maker, note_id)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await _pipeline(maker).apply_intent(
            session,
            note_id=uuid.UUID(note_id),
            note_domain="health",
            captured_at=datetime.now(UTC),
            chunks=chunks,
            intent=intent,
            plan=plan_intent(intent, signals={0: _SURFACE}),
            title="t",
            tags=[],
            extractor=ANALYZER_EXTRACTOR,
            settle_owner=ANALYZER,
            dropped_facts=dropped_facts,
        )


async def _emr_settle(maker, note_id: str) -> None:  # noqa: F811
    """The EMR importer's real commit+settle over the Epic fixture — the producer whose
    every settle took the truncation card's clear branch."""
    chunks = await _load_chunks(maker, note_id)
    anchor = str(chunks[0].id)
    catches = await integrate_parse_result(
        _pipeline(maker),
        maker,
        SYSTEM_CTX,
        note_id=uuid.UUID(note_id),
        note_domain="health",
        captured_at=datetime.now(UTC),
        chunks=chunks,
        result=parse_epic(_EPIC.read_text()),
        chunk_for_anchor=lambda _a: anchor,
    )
    assert catches == []  # the clean Epic fixture emits no location-lock facts


async def _cards(maker, note_id: str, kind: str) -> list[Any]:  # noqa: F811
    """The note's OPEN cards of one kind, with the filer the sweeps now read."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return list(
            (
                await s.execute(
                    select(ReviewItem.id, ReviewItem.payload, ReviewItem.settle_owner).where(
                        ReviewItem.kind == kind,
                        ReviewItem.status == "open",
                        ReviewItem.payload["note_id"].astext == note_id,
                    )
                )
            ).all()
        )


async def _seed_ambiguity(maker, name: str) -> None:  # noqa: F811
    """Two live health entities under one name, so `resolve_entity` returns
    `AmbiguousEntity` and the commit path cards it instead of guessing."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        for _ in range(2):
            entity = Entity(
                id=uuid.uuid4(),
                kind="Organization",
                canonical_name=name,
                status="provisional",
                domain_code="health",
            )
            s.add(entity)
            await s.flush()
            s.add(
                EntityAlias(
                    id=uuid.uuid4(),
                    entity_id=entity.id,
                    alias=name,
                    alias_norm=name.lower(),
                    domain_code="health",
                )
            )


async def _analyzer_ambiguous_card(maker, note_id: str, name: str) -> None:  # noqa: F811
    """File one `ambiguous_mention` card through the analyzer's REAL path: a mention
    the resolver cannot decide goes through `_file_ambiguous_review` inside
    `_resolve_entities`, which every commit path runs."""
    chunks = await _load_chunks(maker, note_id)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await _pipeline(maker).commit_facts(
            session,
            note_id=uuid.UUID(note_id),
            note_domain="health",
            captured_at=datetime.now(UTC),
            chunks=chunks,
            extraction=Extraction(
                title="t",
                tags=[],
                mentions=[ExtractedMention(name=name, kind="Organization", surface_text=name)],
                facts=[],
                tokens=[],
            ),
            extractor=ANALYZER_EXTRACTOR,
            settle_owner=ANALYZER,
        )


async def test_an_emr_settle_leaves_the_analyzers_truncation_card_standing(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The headline bug, in the order the live box produces it.

    The analyzer settles a note whose extraction hit the cap and files the card. The EMR
    importer settles the same note — it always reports `dropped == 0`, because it cannot
    truncate — and the card must survive, because the EMR importer's own extraction
    says nothing whatever about the analyzer's dropped tail.

    Order-independent in practice: the EMR flow is two-stage (stage 1 decrypts and
    re-ingests, stage 2 parses), so `emr_parse` settles at least twice and at least one
    of those lands after `integrate_note`'s. This pins the direction that loses.
    """
    note_id = await _records_note(maker, tmp_path)
    await _analyzer_settle(maker, note_id, dropped_facts=7)
    filed = await _cards(maker, note_id, "extraction_truncated")
    assert len(filed) == 1, "the analyzer's capped extraction files exactly one card"
    assert filed[0].settle_owner == ANALYZER
    assert "skipped 7 facts" in filed[0].payload["summary"]

    await _emr_settle(maker, note_id)

    after = await _cards(maker, note_id, "extraction_truncated")
    assert [r.id for r in after] == [filed[0].id]
    # Untouched, not merely present: the clear branch is a DELETE, so a surviving row
    # with rewritten counts would mean the OTHER branch reached it.
    assert after[0].payload == filed[0].payload


async def test_an_emr_settle_leaves_the_analyzers_ambiguous_card_standing(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The same shape on the quieter half, and the EMR direction is the one where the
    old `NOT IN :names` clause protected nothing at all.

    The analyzer cards a name it could not disambiguate. The EMR importer settles, and
    its `Extraction`'s refs are semantic keys (`org:...`, `cond:...`, `obs:...`) that
    share no surface with that name — so under the old sweep every open card on the note
    was excluded from the spare-list and deleted. Nothing re-files it on the paths that
    matter: `queue.backfill_pending_integration` and `analysis/rebuild.py` re-enqueue
    `integrate_note`/`emr_parse`, never the filer's own resolution pass over that name.
    """
    name = f"Quest Diagnostics {uuid.uuid4().hex[:8]}"
    await _seed_ambiguity(maker, name)
    note_id = await _records_note(maker, tmp_path)
    await _analyzer_ambiguous_card(maker, note_id, name)
    filed = await _cards(maker, note_id, "ambiguous_mention")
    assert [(r.payload["name"], r.settle_owner) for r in filed] == [(name, ANALYZER)]

    await _emr_settle(maker, note_id)

    assert [r.id for r in await _cards(maker, note_id, "ambiguous_mention")] == [filed[0].id]


async def test_a_truncating_settle_does_not_rewrite_a_card_it_did_not_file(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The reverse direction, which mis-fired too: the refresh branch UPDATEd the
    payload of whatever open card the note had, so a producer that truncated overwrote
    another's counts with its own.

    The other producer here is `emr`, which today cannot truncate and so cannot really
    file this card — the row is hand-stamped precisely because no shipped path can
    currently produce this collision. It pins the MECHANISM rather than a live sequence:
    the scoping has to be on the card's filer, so that a second producer which one day
    does run a cap gets its own card instead of trampling the first's.
    """
    note_id = await _records_note(maker, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        payload = {
            "note_id": note_id,
            **truncation_display(kept=40, dropped=3, snippet=None),
        }
        foreign = ReviewItem(
            kind="extraction_truncated",
            payload=payload,
            domain_code="health",
            settle_owner=EMR,
        )
        s.add(foreign)
        await s.flush()
        foreign_id = foreign.id

    await _analyzer_settle(maker, note_id, dropped_facts=11)

    cards = {r.settle_owner: r for r in await _cards(maker, note_id, "extraction_truncated")}
    assert set(cards) == {ANALYZER, EMR}, "the analyzer files its OWN card, beside the other"
    assert cards[EMR].id == foreign_id
    assert cards[EMR].payload == payload
    assert "skipped 11 facts" in cards[ANALYZER].payload["summary"]


async def test_the_emr_import_files_no_truncation_card_because_it_cannot_truncate(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The other half of the bug: EMR's `dropped_facts` was defaulted rather than
    measured, so its `0` read like a finding when nothing had counted.

    It genuinely has nothing to report — the per-note cap is `parse_extraction`'s, on
    the LLM path, and `lower_parse_result` lowers every parsed encounter and orphan
    observation into the one intent with no cap on the way. The two things it declines
    to emit are accounted for elsewhere and are not truncation: a Layer-2 firewall catch
    (carded by `file_firewall_cards`) and a non-committable pathology rule-out. So an
    EMR import on its own must leave the note with no truncation card at all — a card
    here would be a claim about dropped facts that nobody made.
    """
    note_id = await _records_note(maker, tmp_path)
    await _emr_settle(maker, note_id)
    assert await _cards(maker, note_id, "extraction_truncated") == []


async def test_the_filer_still_retires_its_own_stale_cards(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """Scoping must not cost the sweeps their original job, which is the hazard every
    ownership fix carries: a card nobody can retire is as much a defect as one anybody
    can delete.

    Both halves, on one note, each retired by the producer that filed it.
    `_sweep_stale_ambiguous`'s `NOT IN :names` now runs INSIDE the filer's own cards, so
    an analyzer settle whose extraction has stopped referencing the carded name still
    takes that card; and a re-run that fits the fact budget still clears the truncation
    card the earlier, capped run filed.
    """
    name = f"Quest Diagnostics {uuid.uuid4().hex[:8]}"
    await _seed_ambiguity(maker, name)
    note_id = await _records_note(maker, tmp_path)
    await _analyzer_ambiguous_card(maker, note_id, name)
    assert len(await _cards(maker, note_id, "ambiguous_mention")) == 1

    # A capped settle whose extraction no longer references the ambiguous name: it
    # raises the truncation card and retires its own stale ambiguity card in one pass.
    await _analyzer_settle(maker, note_id, dropped_facts=7)
    assert await _cards(maker, note_id, "ambiguous_mention") == []
    assert len(await _cards(maker, note_id, "extraction_truncated")) == 1

    # ...and a re-run that fits the budget clears what it raised.
    await _analyzer_settle(maker, note_id, dropped_facts=0)
    assert await _cards(maker, note_id, "extraction_truncated") == []


async def test_a_resolved_card_is_a_human_decision_and_no_sweep_reopens_it(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The filter the producer key sits beside, pinned so the new clause cannot be read
    as replacing it: `status = 'open'` is what keeps a card the owner already answered
    out of every sweep, whoever settles."""
    note_id = await _records_note(maker, tmp_path)
    await _analyzer_settle(maker, note_id, dropped_facts=7)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "UPDATE app.review_items SET status = 'resolved'"
                " WHERE kind = 'extraction_truncated' AND payload->>'note_id' = :n"
            ),
            {"n": note_id},
        )

    await _analyzer_settle(maker, note_id, dropped_facts=0)

    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                select(ReviewItem.status, ReviewItem.settle_owner).where(
                    ReviewItem.kind == "extraction_truncated",
                    ReviewItem.payload["note_id"].astext == note_id,
                )
            )
        ).all()
    assert [(r.status, r.settle_owner) for r in rows] == [("resolved", ANALYZER)]
