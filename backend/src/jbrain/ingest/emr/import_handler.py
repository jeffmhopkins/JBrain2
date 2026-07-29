"""The EMR parse+integrate job (docs/plans/EMR_IMPORT_PLAN.md §6.3–§6.6).

Runs AFTER intake (§6.1) has decrypted the archive, attached the raw PDFs, and
re-ingested the note so each PDF page is a cited chunk. This handler turns those
decrypted attachments into graph facts, deterministically and with no LLM on the
structured path:

  load PDF attachments → extract text, or vision-OCR a scanned (text-less) PDF
  page by page (§6.2, the one LLM adapter touch here) → dispatch each to its parser
  → reconcile the OCR reprints against the precise draws (§6.4) → integrate each
  precise parse through the shipped arbiter → file a review card for every parked
  OCR read and unrecognized file.

Provenance: each precise source is integrated against ITS OWN attachment chunks,
so a fact's citation lands on the source document (the arbiter anchors an EMR fact
to the head of the chunk set it is handed; honoring the per-page attested span is
a follow-on). All writes run on a health-scoped owner session (§3.6) so provider
resolution can't re-match a general-domain namesake.

Re-run safety: the projections are idempotent — a re-run's retract-and-re-mint
sweep leaves exactly one current `lab_results`/`encounters` row per reading. (The
deterministic path still re-mints graph *entities* on a re-run rather than
matching by a stable EMR key; collapsing those is a follow-on. The user-visible
projection is correct either way — retracted duplicates never surface.)
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.emr.dispatch import Attachment as SourceInput
from jbrain.ingest.emr.dispatch import Source, parse_corpus, select_source
from jbrain.ingest.emr.importer import ChunkResolver
from jbrain.ingest.emr.integrate import file_parked_cards, integrate_parse_result
from jbrain.ingest.emr.onecontent import pdf_word_pages
from jbrain.ingest.emr.reconcile import REVIEW_KIND
from jbrain.ingest.emr.scan_ocr import VISION_OCR_TASK, ocr_scanned_pdf
from jbrain.ingest.extract import KIND_OCR, PdfTextLayerExtractor
from jbrain.ingest.ocr import OCR_CONFIDENCE, OCR_STRENGTH
from jbrain.models.analysis import ReviewItem
from jbrain.models.notes import Attachment, AttachmentExtract, Chunk, Note
from jbrain.storage import BlobStore
from jbrain.workflow.registry import ActionSpec

PDF_MEDIA_TYPE = "application/pdf"
PARAGRAPH = "paragraph"
UNRECOGNIZED_SUBKIND = "emr_unrecognized_source"


def _page_no(anchor: str | None) -> int:
    """The N in a `page N` anchor for ordering; unparseable anchors sort last."""
    if anchor and anchor.lower().startswith("page "):
        try:
            return int(anchor[5:])
        except ValueError:
            pass
    return 1_000_000


# In-code-only action (a migration seeds its trigger). Stage 2 of the EMR import
# (§6.3–§6.6) — parse the decrypted PDFs into graph facts. No LLM on this path.
EMR_PARSE_SPEC = ActionSpec(
    name="emr_parse",
    version=1,
    handler="emr_parse",
    domain_optional=True,
    mutating=True,
    cost_class="standard",
    dedup_key_expr="note_id",
    description="Parse a note's decrypted EMR PDFs into cited lab/encounter facts.",
    category="note",
)

# The plain-read session for note/attachment/chunk lookups (no domain write).
_SYSTEM = SessionContext(principal_id="worker", principal_kind="owner")


class EmrImportPipeline:
    """The `emr_parse` job handler. Constructor-injected deps mirror the shipped
    pipelines (OcrPipeline); `pipeline` is the shared `AnalysisPipeline` whose
    deterministic arbiter (`apply_intent`) commits the lowered candidates."""

    def __init__(
        self, maker: async_sessionmaker, blobs: BlobStore, pipeline: AnalysisPipeline
    ) -> None:
        self._maker = maker
        self._blobs = blobs
        self._pipeline = pipeline
        self._extractor = PdfTextLayerExtractor()

    async def parse(self, payload: dict[str, Any]) -> None:
        note_id = str(payload["note_id"])
        note = await self._load_note(note_id)
        if note is None:
            return
        domain = note.domain_code
        ctx = SessionContext(
            principal_id="worker",
            principal_kind="owner",
            domain_scopes=(domain,),
            owner_scoped=True,  # provider resolution can't reach a general namesake (§3.6)
        )
        attachments = await self._pdf_attachments(note_id)
        if not attachments:
            return
        note_refs, per_attachment = await self._chunk_index(note_id)
        captured_at = note.created_at or datetime.now(UTC)

        sources = await self._build_sources(attachments)
        corpus = parse_corpus(sources)

        for parsed in corpus.precise:
            # Cite THIS attachment's chunks so a fact's provenance lands on the source
            # document's page, not the note body (the arbiter anchors an EMR fact to the
            # head of the chunk set it is given). Fall back to the note chunks if the
            # attachment somehow produced none.
            att_refs, anchors = per_attachment.get(parsed.ref, (note_refs, {}))
            if not att_refs:
                att_refs = note_refs
            resolver = self._resolver(anchors, att_refs)
            await integrate_parse_result(
                self._pipeline,
                self._maker,
                ctx,
                note_id=uuid.UUID(note_id),
                note_domain=domain,
                captured_at=captured_at,
                chunks=att_refs,
                result=parsed.result,
                chunk_for_anchor=resolver,
            )
        await file_parked_cards(
            self._maker,
            ctx,
            note_id=uuid.UUID(note_id),
            note_domain=domain,
            parked=corpus.reconciliation.parked,
        )
        await self._card_unrecognized(ctx, note_id, domain, corpus.unrecognized)

    async def _build_sources(self, attachments: list[Attachment]) -> list[SourceInput]:
        """Extract each decrypted PDF's page text off the event loop, `--- page N ---`
        markered so the parsers' page anchors line up. A PDF WITH a text layer
        (Epic/OneContent/athena) parses that; a scanned PDF with NONE (ARIA) is
        vision-OCR'd page by page (§6.2). Word geometry is derived only for a
        text-layer OneContent — a scan yields linear OCR text, no word boxes."""
        out: list[SourceInput] = []
        for att in attachments:
            data = await self._blobs.get(att.sha256)
            segments = await asyncio.to_thread(self._extractor.extract, data)
            word_pages = None
            if segments:
                text = "\n".join(
                    f"--- {seg.anchor} ---\n{seg.text}" for seg in segments if seg.anchor
                )
                if select_source(text) is Source.ONECONTENT:
                    word_pages = await asyncio.to_thread(pdf_word_pages, data)
            else:
                text = await self._ocr_text(att, data)  # scanned PDF -> vision-OCR (§6.2)
            out.append(SourceInput(text=text, ref=str(att.id), word_pages=word_pages))
        return out

    async def _ocr_text(self, att: Attachment, data: bytes) -> str:
        """The `--- page N ---` markered transcript of a scanned PDF, from the cached
        `ocr` extracts when present (idempotent — a re-run never re-bills the vision
        model) or a fresh vision-OCR pass otherwise, cached per page as it goes."""
        pages = await self._cached_ocr(att.id)
        if pages is None:
            pages = await ocr_scanned_pdf(self._pipeline._router, data, att.filename)
            await self._cache_ocr(att, pages)
        return "\n".join(f"--- page {n} ---\n{t}" for n, t in enumerate(pages, start=1) if t)

    async def _cached_ocr(self, att_id: uuid.UUID) -> list[str] | None:
        """This attachment's cached OCR page transcripts in page order, or None when
        it has never been OCR'd (the two are distinct — an all-blank scan caches as
        empty strings and must not be re-OCR'd)."""
        async with scoped_session(self._maker, _SYSTEM) as s:
            rows = (
                await s.execute(
                    select(AttachmentExtract.source_anchor, AttachmentExtract.text).where(
                        AttachmentExtract.attachment_id == att_id,
                        AttachmentExtract.kind == KIND_OCR,
                    )
                )
            ).all()
        if not rows:
            return None
        by_page = {_page_no(anchor): (text or "") for anchor, text in rows}
        return [by_page[n] for n in sorted(by_page)]

    async def _cache_ocr(self, att: Attachment, pages: list[str]) -> None:
        """Persist one `ocr` extract per page (`source_anchor="page N"`) so the text is
        cited/searchable on the next re-ingest and never re-OCR'd. `tool` stamps the
        model actually used, like the shipped OCR job."""
        if not pages:
            return
        provider, model = await self._pipeline._router.effective_spec(VISION_OCR_TASK, OCR_STRENGTH)
        async with scoped_session(self._maker, _SYSTEM) as s:
            for number, text in enumerate(pages, start=1):
                s.add(
                    AttachmentExtract(
                        attachment_id=att.id,
                        kind=KIND_OCR,
                        tool=f"{provider}:{model}",
                        text=text,
                        confidence=OCR_CONFIDENCE if text else 0.0,
                        source_anchor=f"page {number}",
                        domain_code=att.domain_code,
                    )
                )

    @staticmethod
    def _resolver(anchors: dict[str, uuid.UUID], chunks: list[_ChunkRef]) -> ChunkResolver:
        """Map a parser `page N` anchor to that page's chunk id, falling back to the
        attachment's first chunk. Carried on the intent's `attested_span`; the arbiter
        currently anchors an EMR fact to the head of its chunk set (per-page honoring
        of the attested span is a follow-on), so passing the attachment's own chunks
        is what lands the citation on the right document."""
        fallback = str(chunks[0].id) if chunks else ""

        def resolve(anchor: str) -> str:
            chunk_id = anchors.get(anchor)
            return str(chunk_id) if chunk_id is not None else fallback

        return resolve

    async def _load_note(self, note_id: str) -> Note | None:
        async with scoped_session(self._maker, _SYSTEM) as s:
            return (
                await s.execute(select(Note).where(Note.id == uuid.UUID(note_id)))
            ).scalar_one_or_none()

    async def _pdf_attachments(self, note_id: str) -> list[Attachment]:
        async with scoped_session(self._maker, _SYSTEM) as s:
            return list(
                (
                    await s.execute(
                        select(Attachment).where(
                            Attachment.note_id == uuid.UUID(note_id),
                            Attachment.media_type == PDF_MEDIA_TYPE,
                        )
                    )
                )
                .scalars()
                .all()
            )

    async def _chunk_index(
        self, note_id: str
    ) -> tuple[list[_ChunkRef], dict[str, tuple[list[_ChunkRef], dict[str, uuid.UUID]]]]:
        """The note's paragraph chunks as a fallback `_ChunkRef` set, plus, per
        attachment, its own ordered chunks + a `page N` → first-chunk-id map (the
        citation set and resolver each integrate call is scoped to)."""
        async with scoped_session(self._maker, _SYSTEM) as s:
            rows = (
                await s.execute(
                    select(Chunk.id, Chunk.text, Chunk.attachment_id, Chunk.source_anchor)
                    .where(Chunk.note_id == uuid.UUID(note_id), Chunk.granularity == PARAGRAPH)
                    .order_by(Chunk.seq)
                )
            ).all()
        note_refs = [_ChunkRef(id=r.id, text=r.text) for r in rows]
        per_attachment: dict[str, tuple[list[_ChunkRef], dict[str, uuid.UUID]]] = {}
        for r in rows:
            if r.attachment_id is None:
                continue
            key = str(r.attachment_id)
            refs, anchors = per_attachment.setdefault(key, ([], {}))
            refs.append(_ChunkRef(id=r.id, text=r.text))
            if r.source_anchor:
                anchors.setdefault(r.source_anchor, r.id)  # first chunk per page (min seq)
        return note_refs, per_attachment

    async def _card_unrecognized(
        self, ctx: SessionContext, note_id: str, domain: str, refs: list[str]
    ) -> None:
        """A file that matched no parser fingerprint is routed to review (§6.3), never
        free-extracted. One open card per (note, attachment)."""
        if not refs:
            return
        async with scoped_session(self._maker, ctx) as s:
            for ref in refs:
                s.add(
                    ReviewItem(
                        kind=REVIEW_KIND,
                        payload={
                            "note_id": note_id,
                            "subkind": UNRECOGNIZED_SUBKIND,
                            "attachment_id": ref,
                        },
                        domain_code=domain,
                    )
                )
