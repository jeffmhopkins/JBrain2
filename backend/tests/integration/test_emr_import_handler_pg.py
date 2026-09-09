"""The EMR parse+integrate job end-to-end (docs/plans/EMR_IMPORT_PLAN.md §6.3–§6.6)
against real Postgres. A decrypted athena PDF is attached to a health note and
ingested (so each page becomes a cited chunk); the `emr_parse` handler then
extracts → dispatches → parses → integrates, minting graph facts + the
`lab_results` projection, with every fact citing a REAL attachment page chunk —
and, with Layer-1 stripping disabled, holding a whereabouts fact out of the graph
AND telling the owner it did (§3.6).
"""

import json
import uuid
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import func, select, text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.ingest.emr import importer
from jbrain.ingest.emr.firewall import FIREWALL_REVIEW_KIND, FIREWALL_REVIEW_SUBKIND
from jbrain.ingest.emr.import_handler import UNRECOGNIZED_SUBKIND, EmrImportPipeline
from jbrain.ingest.emr.importer import FirewallCatch
from jbrain.ingest.emr.integrate import file_firewall_cards
from jbrain.ingest.emr.reconcile import REVIEW_KIND
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, Fact, ReviewItem
from jbrain.models.notes import Attachment, Chunk
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_ATHENA = Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "athena_panel.txt"


def _pdf_from_lines(text_body: str) -> bytes:
    """A one-page PDF whose text layer reproduces the fixture's lines (minus the
    `--- page N ---` marker), one line per row — so get_text('text') round-trips the
    structure the parser reads."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=1500)
    y = 40.0
    for line in text_body.splitlines():
        if line.strip().startswith("--- page"):
            continue
        if line.strip():
            page.insert_text((40, y), line, fontsize=9)
        y += 14.0
    out = doc.tobytes()
    doc.close()
    return out


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    return AnalysisPipeline(maker, router)


async def _attach_pdf(maker, blobs, note_id: str, data: bytes) -> uuid.UUID:  # noqa: F811
    sha = await blobs.put(data)
    att_id = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        s.add(
            Attachment(
                id=att_id,
                note_id=uuid.UUID(note_id),
                domain_code="health",
                sha256=sha,
                filename="labs.pdf",
                media_type="application/pdf",
                size_bytes=len(data),
            )
        )
    return att_id


async def test_emr_parse_job_mints_facts_citing_real_page_chunks(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported athena labs.")
    att_id = await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    # Ingest chunks the attached PDF: each page -> a text-layer chunk anchored "page N".
    await ingest(maker, note_id, tmp_path)

    await EmrImportPipeline(maker, blobs, _pipeline(maker)).parse({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as s:
        # The athena panel minted its ambulatory encounter + lab observations.
        obs = (
            (await s.execute(select(Entity).where(Entity.canonical_name == "Platelet count")))
            .scalars()
            .all()
        )
        assert obs and all(e.domain_code == "health" for e in obs)
        enc = (
            (await s.execute(select(Entity).where(func.lower(Entity.kind) == "encounter")))
            .scalars()
            .all()
        )
        assert enc

        # The lab_results projection is populated for the platelet reading.
        plt = (
            await s.execute(
                text("SELECT value_num FROM app.lab_results WHERE analyte = 'Platelet count'")
            )
        ).all()
        assert plt and plt[0][0] == 188.0

        # Every value fact cites a REAL chunk that belongs to the PDF attachment's page,
        # not a stub — the citation the arbiter froze points at the page it was printed on.
        value_facts = (
            (
                await s.execute(
                    select(Fact).where(
                        Fact.note_id == uuid.UUID(note_id),
                        Fact.predicate == "value",
                        Fact.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert value_facts
        cited_chunk_ids = {f.chunk_id for f in value_facts if f.chunk_id is not None}
        assert cited_chunk_ids
        att_chunk_ids = {
            r[0]
            for r in (
                await s.execute(
                    select(Chunk.id).where(
                        Chunk.attachment_id == att_id, Chunk.source_kind == "text-layer"
                    )
                )
            ).all()
        }
        assert cited_chunk_ids <= att_chunk_ids  # every citation is a real page chunk


async def test_emr_parse_job_is_idempotent(maker, tmp_path):  # noqa: F811
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported athena labs.")
    await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    await ingest(maker, note_id, tmp_path)
    handler = EmrImportPipeline(maker, blobs, _pipeline(maker))
    await handler.parse({"note_id": note_id})
    await handler.parse({"note_id": note_id})  # a safe re-run: no crash, projection stays correct
    async with scoped_session(maker, SYSTEM_CTX) as s:
        # The projection is idempotent: exactly one current platelet row and one
        # encounter FOR THIS NOTE, regardless of the re-run's retract-and-re-mint sweep.
        plt = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.lab_results"
                    " WHERE analyte = 'Platelet count' AND source_note_id = :n AND is_current"
                ),
                {"n": note_id},
            )
        ).scalar_one()
        assert plt == 1
        encs = (
            await s.execute(
                text("SELECT count(*) FROM app.encounters WHERE source_note_id = :n"),
                {"n": note_id},
            )
        ).scalar_one()
        assert encs == 1


# Synthetic — the whereabouts a parser regression would hand the importer.
_LEAKED_ADDRESS = "1200 Elm Street, Springfield IL 62701"


@pytest.fixture
def stripping_disabled(monkeypatch):
    """Layer 1 off: every parsed encounter also carries the facility's postal
    address, exactly as a parser regression would emit it (§9). Layer 2 alone must
    then hold it out AND card it."""
    original = importer._add_encounter

    def leaky(b, enc, enc_ref_by_key):
        original(b, enc, enc_ref_by_key)
        b.fact(
            entity_ref=enc_ref_by_key[enc.key],
            entity_kind=importer.KIND_ENCOUNTER,
            predicate="address",
            qualifier="",
            kind="attribute",
            statement=_LEAKED_ADDRESS,
            anchor=enc.source_anchor,
            value_json={"value": _LEAKED_ADDRESS},
        )

    monkeypatch.setattr(importer, "_add_encounter", leaky)


async def _firewall_cards(s, note_id: str) -> list[ReviewItem]:
    """This note's firewall cards, whatever their status — the dedup contract is
    across ALL statuses, so an open-only read would hide the bug it guards against
    (the store is shared across the module's tests, hence the note scoping)."""
    rows = (
        (await s.execute(select(ReviewItem).where(ReviewItem.kind == FIREWALL_REVIEW_KIND)))
        .scalars()
        .all()
    )
    return [
        r
        for r in rows
        if r.payload.get("subkind") == FIREWALL_REVIEW_SUBKIND
        and r.payload.get("note_id") == note_id
    ]


async def test_firewall_catch_is_held_out_of_the_graph_and_carded(
    maker,  # noqa: F811
    tmp_path,
    stripping_disabled,
):
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported athena labs.")
    att_id = await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    await ingest(maker, note_id, tmp_path)

    handler = EmrImportPipeline(maker, blobs, _pipeline(maker))
    await handler.parse({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as s:
        # (a) The fact is still never committed — no address edge, and the value
        # appears nowhere in the graph the guard is protecting.
        assert (
            await s.execute(text("SELECT count(*) FROM app.facts WHERE predicate = 'address'"))
        ).scalar_one() == 0
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.facts WHERE statement LIKE :a"),
                {"a": "%Elm Street%"},
            )
        ).scalar_one() == 0

        # (b) ...and the owner is now told: one card, health domain, right subkind.
        cards = await _firewall_cards(s, note_id)
        assert len(cards) == 1
        card = cards[0]
        assert card.domain_code == "health"
        assert card.status == "open"
        assert card.payload["subkind"] == FIREWALL_REVIEW_SUBKIND
        assert card.payload["predicate"] == "address"
        assert card.payload["entity_kind"] == importer.KIND_ENCOUNTER
        assert card.payload["note_id"] == note_id
        assert card.payload["attachment_id"] == str(att_id)
        assert card.payload["anchor"] == "page 1"
        assert card.payload["count"] == 1
        # The key SET is pinned rather than the payload searched for the leaked string:
        # the realistic regression is a future `snippet=mark_snippet(chunk.text)`, real
        # page text that carries the real address in production while this fixture's is
        # monkeypatched in and absent from the PDF — a substring probe passes green
        # while the leak ships. Adding a key here must therefore be a deliberate
        # decision; `snippet`/`statement`/`value_json` never are, because this card sits
        # in the very domain the value was held out of.
        assert set(card.payload) == {
            "note_id",
            "subkind",
            "key",
            "attachment_id",
            "anchor",
            "predicate",
            "entity_kind",
            "count",
            "summary",
            "rationale",
            "choices",
            "correctable",
        }
        assert "Elm" not in json.dumps(card.payload)
        # Evidence that a control fired, plus an exit that does not undo it. A card
        # carrying no `choices` renders with NO buttons at all (frontend proposalsFor),
        # leaving the footer's "correct it" — which files an owner_correction note in
        # THIS card's health domain, force-superseded and pinned — as the only way out.
        # So `dismiss` is advertised explicitly and the correction footer is suppressed.
        assert card.payload["choices"] == [
            {
                "action": "dismiss",
                "label": "Dismiss",
                "detail": "the held fact stays out of the health graph",
            }
        ]
        assert card.payload["correctable"] is False
        assert "outcomes" not in card.payload


async def test_firewall_card_does_not_re_file_or_come_back_once_dismissed(
    maker,  # noqa: F811
    tmp_path,
    stripping_disabled,
):
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported athena labs.")
    await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    await ingest(maker, note_id, tmp_path)
    handler = EmrImportPipeline(maker, blobs, _pipeline(maker))

    await handler.parse({"note_id": note_id})
    await handler.parse({"note_id": note_id})  # re-import of the same source
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert len(await _firewall_cards(s, note_id)) == 1

        # Dismissed is a decision, not a snooze: the dedup spans ALL statuses, so the
        # next import must not resurrect the card (the wiki-lint open-only bug).
        await s.execute(
            text(
                "UPDATE app.review_items SET status = 'dismissed'"
                " WHERE kind = :k AND payload->>'subkind' = :sk"
                " AND payload->>'note_id' = :n"
            ),
            {"k": FIREWALL_REVIEW_KIND, "sk": FIREWALL_REVIEW_SUBKIND, "n": note_id},
        )

    await handler.parse({"note_id": note_id})
    async with scoped_session(maker, SYSTEM_CTX) as s:
        cards = await _firewall_cards(s, note_id)
        assert len(cards) == 1
        assert cards[0].status == "dismissed"


async def test_repeated_catches_collapse_into_a_counted_card_but_distinct_ones_do_not(
    maker,  # noqa: F811
):
    """The in-flight half of the dedup: identical catches in a SINGLE call must not
    double-file, which the not-yet-flushed existence probe cannot see on its own. A
    different locked predicate on the same page is a different catch and still gets
    its own card — and the collapsed one reports HOW MANY it stands for, because the
    anchor is page-granular and a page carries several encounters: a control that
    says "1 fact was held" when it held three under-reports how often it fired."""
    note_id = await make_note(maker, domain="health", body="Imported athena labs.")
    address = FirewallCatch(entity_kind="Encounter", predicate="address", anchor="page 1")
    geo = FirewallCatch(entity_kind="Encounter", predicate="geo", anchor="page 1")
    filed = await file_firewall_cards(
        maker,
        SYSTEM_CTX,
        note_id=uuid.UUID(note_id),
        note_domain="health",
        catches=[("att-1", address), ("att-1", address), ("att-1", address), ("att-1", geo)],
    )
    assert filed == 2
    async with scoped_session(maker, SYSTEM_CTX) as s:
        by_predicate = {c.payload["predicate"]: c for c in await _firewall_cards(s, note_id)}
        assert set(by_predicate) == {"address", "geo"}
        assert by_predicate["address"].payload["count"] == 3
        assert by_predicate["geo"].payload["count"] == 1
        # ...and the hero line the owner reads says three, in the predicate that was
        # actually caught (not a hard-coded "a address").
        assert "3 address facts" in by_predicate["address"].payload["summary"]
        assert "1 geo fact " in by_predicate["geo"].payload["summary"]


async def _unrecognized_cards(s, note_id: str) -> list[ReviewItem]:
    """This note's unrecognized-source cards, whatever their status (the store is
    shared across the module's tests, hence the note scoping)."""
    rows = (
        (await s.execute(select(ReviewItem).where(ReviewItem.kind == REVIEW_KIND))).scalars().all()
    )
    return [
        r
        for r in rows
        if r.payload.get("subkind") == UNRECOGNIZED_SUBKIND and r.payload.get("note_id") == note_id
    ]


async def test_unrecognized_source_cards_once_and_stays_dismissed(
    maker,  # noqa: F811
    tmp_path,
):
    """A file matching no parser fingerprint routes to review — ONCE. `emr_parse`
    re-runs on every re-ingest, so with no existence probe the handler minted a fresh
    row per unrecognized file per run, while its docstring claimed one card per
    attachment; an open-only probe would then read the owner's dismissal as a snooze.
    The athena fixture yields no unrecognized source, which is why the idempotency test
    above is blind to this — hence a note whose PDF matches no fingerprint."""
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="A leaflet, not a lab report.")
    att_id = await _attach_pdf(
        maker,
        blobs,
        note_id,
        _pdf_from_lines("Dental hygiene leaflet\nBrush twice daily.\nFloss once daily."),
    )
    await ingest(maker, note_id, tmp_path)
    handler = EmrImportPipeline(maker, blobs, _pipeline(maker))

    await handler.parse({"note_id": note_id})
    await handler.parse({"note_id": note_id})  # a re-ingest re-runs the job
    async with scoped_session(maker, SYSTEM_CTX) as s:
        cards = await _unrecognized_cards(s, note_id)
        assert len(cards) == 1
        assert cards[0].payload["attachment_id"] == str(att_id)
        assert cards[0].domain_code == "health"

        await s.execute(
            text(
                "UPDATE app.review_items SET status = 'dismissed'"
                " WHERE kind = :k AND payload->>'subkind' = :sk AND payload->>'note_id' = :n"
            ),
            {"k": REVIEW_KIND, "sk": UNRECOGNIZED_SUBKIND, "n": note_id},
        )

    await handler.parse({"note_id": note_id})
    async with scoped_session(maker, SYSTEM_CTX) as s:
        cards = await _unrecognized_cards(s, note_id)
        assert len(cards) == 1
        assert cards[0].status == "dismissed"
