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
from tests.integration.test_extraction_pg import (  # noqa: F401
    analyzer,
    extraction_payload,
    ingest,
    make_note,
    maker,
)
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


# --- one note, one settle (W4/D9, plan constraint 6) --------------------------


_EPIC = Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "epic_report.txt"


async def _sourcing_attachments(s, note_id: str) -> set[str]:
    """The attachments this note's ACTIVE facts still cite — which SOURCE survived the
    settle.

    Keyed on the citation rather than on analyte names because the two fixtures overlap:
    athena's three analytes are all present in the Epic report too, so "Potassium is
    live" is satisfied by either source and proves nothing. Each source is integrated
    against ITS OWN attachment chunks (`import_handler._chunk_index`), so the cited
    chunk's `attachment_id` is the one thing that says which PDF a live fact came from.
    """
    rows = (
        await s.execute(
            text(
                "SELECT DISTINCT c.attachment_id FROM app.facts f"
                " JOIN app.chunks c ON c.id = f.chunk_id"
                " WHERE f.note_id = :n AND f.status = 'active'"
                "   AND c.attachment_id IS NOT NULL"
            ),
            {"n": note_id},
        )
    ).all()
    return {str(r[0]) for r in rows}


async def test_two_emr_attachments_on_one_note_both_survive_the_settle(maker, tmp_path):  # noqa: F811
    """The bug the W4 port fixes, and the reason the importer needs W1's seam.

    A decrypted EMR archive attaches MANY PDFs to ONE note, and each is dispatched to
    its own parser and lowered to its own intent. Committing those through `apply_intent`
    in a loop ran `settle_note` per attachment — and `settle_note` is whole-note: it
    retracts every non-pinned fact of the note it is not told about (plan constraint 6).
    So the second PDF's settle retracted the first PDF's readings, and a two-source
    import kept only the last source's. Nothing caught it because every EMR test in the
    suite attached exactly one file.

    `EmrNoteCommit` accumulates `touched`/`projected`/`mention_ids` across every source
    and settles ONCE. The two fixtures OVERLAP in their analytes — Potassium, Creatinine
    and Platelet count are in both — so an analyte-name assertion would be satisfied by
    either source alone and would prove nothing. `_sourcing_attachments` discriminates on
    the CITED CHUNK's attachment id instead, which is the only thing that says which PDF
    a live fact came from."""
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported EMR records.")
    a_athena = await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    a_epic = await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_EPIC.read_text()))
    await ingest(maker, note_id, tmp_path)

    await EmrImportPipeline(maker, blobs, _pipeline(maker)).parse({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as s:
        sourced = await _sourcing_attachments(s, note_id)
        assert sourced == {str(a_athena), str(a_epic)}, (
            "a source's facts were swept by another source's settle; live citations"
            f" name only {sorted(sourced)}"
        )

        # And the note settled exactly once — one `note_analysis` stamp, which is also
        # the PWA's "analyzed" watermark.
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.note_analysis WHERE note_id = :n"), {"n": note_id}
            )
        ).scalar_one() == 1


async def test_fhir_status_still_reaches_decide_through_the_accumulating_path(maker, tmp_path):  # noqa: F811
    """`fhir_status` is the field that makes the importer/tool boundary non-negotiable
    (TOOL_SURFACE gap 3): it is EMR-only, set by the parser, has no `assert_fact` field,
    and is what `supersession._lab_status_transition` reads.

    The Epic fixture's platelet reading arrives `status corrected` with no prior reading
    at its draw — the §3.5 red-team shape, which the status-aware transition holds for
    review and a status-blind commit would make a live current value. Asserted HERE, on
    the multi-source handler path, because that is the path W4 re-plumbed onto
    `commit_intent`; `test_emr_integrate_pg` proves the same thing for a single source."""
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported EMR records.")
    await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_EPIC.read_text()))
    await ingest(maker, note_id, tmp_path)

    await EmrImportPipeline(maker, blobs, _pipeline(maker)).parse({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as s:
        held = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.facts f"
                    " JOIN app.entities e ON e.id = f.entity_id"
                    " WHERE f.note_id = :n AND f.predicate = 'value'"
                    "   AND e.canonical_name = 'Platelet count'"
                    "   AND f.status = 'pending_review'"
                ),
                {"n": note_id},
            )
        ).scalar_one()
        # ...and the held row was NOT swept by the one settle: `commit_facts` puts a
        # pending_review id into `touched` precisely so the whole-note sweep spares it.
        assert held >= 1, "the `corrected` platelet went live — fhir_status was lost"


async def test_layer_2_holds_across_sources_and_the_shared_settle(
    maker,  # noqa: F811
    tmp_path,
    stripping_disabled,
):
    """Layer 2 is a hard NON-COMMIT, and the port must not have made it a filter.

    The guard runs inside `lower_parse_result`, on the way from a parser candidate to an
    `IntentFact` — so a location-locked predicate never becomes a fact any commit path
    can see, and there is nothing downstream (no plan, no `commit_intent`, no settle) to
    un-hold. This is the multi-source version of the single-attachment test above: a
    second source commits AFTER the catch and the one whole-note settle runs after both,
    and the address is still absent from the graph while both catches are carded."""
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported EMR records.")
    a_athena = await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    a_epic = await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_EPIC.read_text()))
    await ingest(maker, note_id, tmp_path)

    await EmrImportPipeline(maker, blobs, _pipeline(maker)).parse({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as s:
        # Both sources committed and survived, so the settle really did run over both.
        assert await _sourcing_attachments(s, note_id) == {str(a_athena), str(a_epic)}

        # Nothing whereabouts-shaped reached the graph, in ANY status — a `retracted` or
        # `pending_review` address row would still be the value sitting in the health
        # domain, which is exactly what Layer 2 exists to prevent.
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.facts WHERE note_id = :n AND predicate = 'address'"),
                {"n": note_id},
            )
        ).scalar_one() == 0
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.facts WHERE note_id = :n AND statement LIKE :a"),
                {"n": note_id, "a": "%Elm Street%"},
            )
        ).scalar_one() == 0

        # ...and the owner is told, once per attachment the guard fired in.
        cards = await _firewall_cards(s, note_id)
        assert {c.payload["attachment_id"] for c in cards} == {str(a_athena), str(a_epic)}
        assert all(c.payload["predicate"] == "address" for c in cards)
        assert all("Elm" not in json.dumps(c.payload) for c in cards)


# --- the OTHER settle collision, closed by the producer key -------------------


async def test_the_generic_integrator_does_not_retract_the_emr_parse_it_races(maker, tmp_path):  # noqa: F811
    """Both producers' facts survive one note.

    `note.ingested` on a health `Records` note fans out to BOTH `integrate_note` (0040)
    and `emr_parse` (0122), and each ends in `settle_note` — so whichever ran second did
    not merely write late, it RETRACTED the other's facts. That was the shipped
    `apply_intent`, on both sides, and W4's EMR half had fixed only the collision BETWEEN
    EMR sources.

    It is closed by the same change that closed the conversation's half, without a
    special case for EMR: the sweep is scoped to the rows the settling producer stamped
    (`analysis/settle_owner.py`, docs/plans/SETTLE_OWNERSHIP.md S1), so the analyzer
    retracts `analyzer` rows and the importer `emr` rows and neither can reach the other.
    D10's tool-set difference (`ingest/emr/ownership.py`) still keeps the CONVERSATION
    from writing here at all, which is why this note has two producers and not three.

    `_IntegrateDriver` is the real `integrate_note` path with the two model calls
    scripted, so what runs here is the shipped extraction → arbiter → apply, not a
    stand-in for it. The extraction deliberately says nothing about the labs: the point
    is that a settle does not need to CONTRADICT the EMR facts to retract them, only to
    not mention them.
    """
    blobs = FsBlobStore(tmp_path)
    note_id = await make_note(maker, domain="health", body="Imported athena labs.")
    await _attach_pdf(maker, blobs, note_id, _pdf_from_lines(_ATHENA.read_text()))
    await ingest(maker, note_id, tmp_path)

    await EmrImportPipeline(maker, blobs, _pipeline(maker)).parse({"note_id": note_id})
    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await _sourcing_attachments(s, note_id)  # the EMR facts are live

    # ...and now the OTHER producer of the same event runs.
    # TWO scripted extractions, not one: the note's chunks come from two SOURCES (the
    # body and the PDF attachment), and `_extract_note` makes one model call per source
    # group. Scripting one leaves the second call to consume the intent JSON and die on a
    # missing `title` — which xfails this test for a reason that has nothing to do with
    # the settle it is about.
    extract = json.dumps(extraction_payload())
    await analyzer(maker, [extract, extract]).analyze_note({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as s:
        assert await _sourcing_attachments(s, note_id), (
            "integrate_note's whole-note settle retracted every fact emr_parse wrote"
        )
