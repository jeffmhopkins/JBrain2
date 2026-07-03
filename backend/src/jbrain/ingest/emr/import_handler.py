"""The EMR parse+integrate job (docs/plans/EMR_IMPORT_PLAN.md §6.3–§6.6).

Runs AFTER intake (§6.1) has decrypted the archive, attached the raw PDFs, and
re-ingested the note so each PDF page is a cited chunk. This handler turns those
decrypted attachments into graph facts, deterministically and with no LLM on the
structured path:

  load PDF attachments → extract text (+ word geometry for OneContent) → dispatch
  each to its parser → reconcile the OCR reprints against the precise draws (§6.4)
  → integrate each precise parse through the shipped arbiter, citing the real
  page chunk → file a review card for every parked OCR read and unrecognized file.

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
from jbrain.ingest.extract import PdfTextLayerExtractor
from jbrain.models.analysis import ReviewItem
from jbrain.models.notes import Attachment, Chunk, Note
from jbrain.storage import BlobStore

PDF_MEDIA_TYPE = "application/pdf"
PARAGRAPH = "paragraph"
UNRECOGNIZED_SUBKIND = "emr_unrecognized_source"

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
        """Extract each decrypted PDF's page text (+ word geometry when the source is
        OneContent) off the event loop. Text carries `--- page N ---` markers so the
        parsers' page anchors line up with the chunk index."""
        out: list[SourceInput] = []
        for att in attachments:
            data = await self._blobs.get(att.sha256)
            segments = await asyncio.to_thread(self._extractor.extract, data)
            text = "\n".join(f"--- {seg.anchor} ---\n{seg.text}" for seg in segments if seg.anchor)
            word_pages = None
            if select_source(text) is Source.ONECONTENT:
                word_pages = await asyncio.to_thread(pdf_word_pages, data)
            out.append(SourceInput(text=text, ref=str(att.id), word_pages=word_pages))
        return out

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
