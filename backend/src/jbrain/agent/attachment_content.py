"""Turning a chat turn's pre-uploaded attachments into adapter-agnostic LLM content.

Given the session's narrowed read context and the ordered attachment ids the turn
referenced, this returns an `AttachmentContent`: vision images for the model plus a
labeled text block to append to the user message. An image rides the turn at its
cache-stable ANCHOR position and stays in view for the carry window (see
`anchored_image_content` / api.agent); PDF pages and text blocks live one turn.

Binding decisions (owner): images go straight to vision; a PDF is BOTH rasterized
per page (each page → a PNG LlmImage) AND has its text layer extracted; known-text
files are decoded inline. Everything is fetched under the session's firewall via
TurnAttachmentRepo.get, so an out-of-scope or unknown id is invisible (it reads as
missing) and is skipped rather than crashing the turn (CLAUDE.md rules 2 & 3).

The rasterize/extract work is synchronous CPU work (PyMuPDF), so it runs off the
event loop via asyncio.to_thread.
"""

import asyncio
import base64
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

import pymupdf

from jbrain.agent.attachments import AttachmentInfo, TurnAttachmentRepo
from jbrain.db.session import SessionContext
from jbrain.llm import LlmImage
from jbrain.storage import BlobStore

_log = logging.getLogger(__name__)

# How many attachment ids one turn may reference — a graceful cap mirrored by the
# request validator. Keeps a turn from ballooning the context with dozens of files.
MAX_ATTACHMENTS_PER_TURN = 10
# Per PDF, how many pages we rasterize + extract. A long PDF is truncated (with a
# note appended) rather than flooding the vision context.
MAX_PDF_PAGES = 10
# The overall image cap for one turn (images from images + PDF pages combined), so a
# handful of multi-page PDFs can't exceed what the vision model should receive.
MAX_IMAGES_PER_TURN = 20
# Pages render at this zoom (1.0 = 72 dpi); ~1.5x keeps text legible without bloating
# the base64 payload.
_PDF_RENDER_ZOOM = 1.5
# The ceiling on a rendered page's pixel area (~4 MP). A malicious PDF can declare a
# huge MediaBox (a 522-byte file → a 21600x21600 ≈ 2.7 GB pixmap) that the page-count
# and byte caps don't bound; the per-page zoom is floored so the pixmap never exceeds
# this budget (SECURITY: rasterization DoS).
MAX_PDF_PAGE_PIXELS = 4_000_000


@dataclass(frozen=True)
class _Converted:
    images: list[LlmImage]
    text_blocks: list[str]


def _image_block(info: AttachmentInfo, data: bytes, *, can_see_images: bool) -> _Converted:
    image = LlmImage(media_type=info.media_type, data=_b64(data))
    # The note's wording follows the SAME vision gate that decides whether the bytes ride
    # inline (api.agent). When the turn model is vision-capable the bytes ARE in view, so
    # tell it to look directly — otherwise it dutifully delegates to analyze_image while its
    # own perception leaks into the tool args (it "can't see" yet describes the image), a
    # self-contradiction that also pays a redundant vision round-trip. When it's text-only
    # the bytes were dropped, so the id-by-reference pointer is the only way in.
    if can_see_images:
        note = (
            f'[attached image "{info.filename}" — its id is {info.id}. You can see this '
            "image directly here: describe and reason about it yourself, do NOT say you "
            "can't see it. Use analyze_image ONLY to read its exact text (OCR).]"
        )
    else:
        note = (
            f'[attached image "{info.filename}" — its id is {info.id}: pass it as '
            "source_attachment_id to analyze_image to look at it]"
        )
    return _Converted(images=[image], text_blocks=[note])


def _text_block(info: AttachmentInfo, data: bytes) -> _Converted:
    body = data.decode("utf-8", errors="replace").strip()
    if not body:
        return _Converted(images=[], text_blocks=[])
    return _Converted(images=[], text_blocks=[f"[{info.filename}]:\n{body}"])


def _media_block(info: AttachmentInfo, *, kind: str, transcribe_enabled: bool) -> _Converted:
    # The model can't hear/watch the bytes, so only its id rides the turn (no inline
    # data). When the whisper backend is configured the id is actionable via the
    # transcribe tool (a video's audio track is extracted gateway-side); when it
    # isn't, say so plainly rather than pointing at a dropped tool (no dead-end).
    if transcribe_enabled:
        note = (
            f'[attached {kind} "{info.filename}" — its id is {info.id}: pass it as '
            "source_attachment_id to the transcribe tool to read what it says]"
        )
    else:
        note = (
            f'[attached {kind} "{info.filename}" — transcription is not configured '
            "on this box, so its words can't be read]"
        )
    return _Converted(images=[], text_blocks=[note])


def _pdf_block(info: AttachmentInfo, data: bytes, image_budget: int) -> _Converted:
    """Each page (up to MAX_PDF_PAGES and the remaining image budget) → a PNG image
    for vision AND its extracted text layer. Synchronous PyMuPDF work — the caller
    runs it via asyncio.to_thread."""
    images: list[LlmImage] = []
    text_blocks: list[str] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        page_cap = min(doc.page_count, MAX_PDF_PAGES)
        for number in range(1, page_cap + 1):
            page = doc.load_page(number - 1)
            if len(images) < image_budget:
                png = page.get_pixmap(matrix=_page_matrix(page)).tobytes("png")
                images.append(LlmImage(media_type="image/png", data=_b64(png)))
            page_text = page.get_text("text").strip()  # type: ignore[no-untyped-call]
            if page_text:
                text_blocks.append(f"[{info.filename}, page {number}]:\n{page_text}")
        if doc.page_count > page_cap:
            text_blocks.append(
                f"[{info.filename}]: showing the first {page_cap} of {doc.page_count} pages."
            )
    return _Converted(images=images, text_blocks=text_blocks)


def _page_matrix(page: pymupdf.Page) -> pymupdf.Matrix:
    """The render matrix for one page: the base zoom, floored so the rasterized pixel
    area stays within MAX_PDF_PAGE_PIXELS. The zoom is only ever REDUCED — a normal
    page renders at the base zoom; an oversized MediaBox is scaled down so it can't
    blow up the pixmap (SECURITY: rasterization DoS). Pixels scale with zoom², so the
    cap is sqrt(budget / area_in_points)."""
    area_pt = max(page.rect.width * page.rect.height, 1.0)
    zoom = min(_PDF_RENDER_ZOOM, math.sqrt(MAX_PDF_PAGE_PIXELS / area_pt))
    zoom = max(zoom, 1e-3)  # keep it positive even for an absurdly large page
    return pymupdf.Matrix(zoom, zoom)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _convert_one(
    info: AttachmentInfo,
    data: bytes,
    image_budget: int,
    *,
    transcribe_enabled: bool,
    can_see_images: bool,
) -> _Converted:
    """Route one attachment to its conversion by media type. CPU-bound for PDFs, so
    the caller invokes this off the event loop (asyncio.to_thread)."""
    if info.media_type.startswith("image/"):
        return (
            _image_block(info, data, can_see_images=can_see_images)
            if image_budget > 0
            else _Converted([], [])
        )
    if info.media_type == "application/pdf":
        return _pdf_block(info, data, image_budget)
    if info.media_type.startswith("audio/"):
        # Binary audio has no inline reading — decoding it as text would be garbage;
        # surface its id (and whether it's actionable) instead.
        return _media_block(info, kind="audio", transcribe_enabled=transcribe_enabled)
    if info.media_type.startswith("video/"):
        # Same as audio: unreadable inline, but transcribable (the gateway extracts
        # the audio track) — surface its id pointing at the transcribe tool.
        return _media_block(info, kind="video", transcribe_enabled=transcribe_enabled)
    # Everything else reaching here is a known-text type (the upload allowlist gates
    # the set); decode it inline.
    return _text_block(info, data)


async def anchored_image_content(
    blobs: BlobStore, infos: Sequence[AttachmentInfo], *, image_budget: int
) -> tuple[list[LlmImage], str]:
    """One history-anchored message body for the images of ONE earlier turn: `(images,
    note)` the caller inserts as its own user message DIRECTLY AFTER that turn in
    history. Everything here must be DETERMINISTIC across turns — the note text and the
    byte-exact blobs — because the whole point of the anchor is that the gateway's KV
    prefix cache matches this message verbatim turn-over-turn (llama-server compares a
    media chunk by content hash + position), so the image is encoded once and then
    rides the cached prefix instead of re-costing vision every turn. Capped at
    `image_budget`; a blob that outlived its row is skipped; `("", ...)` note when
    nothing survives."""
    images: list[LlmImage] = []
    names: list[str] = []
    for info in infos:
        if len(images) >= image_budget:
            break
        try:
            data = await blobs.get(info.sha256)
        except FileNotFoundError:
            continue  # row outlived its blob — skip, like build_attachment_content
        images.append(LlmImage(media_type=info.media_type, data=_b64(data)))
        names.append(f'"{info.filename}" (id {info.id})')
    if not images:
        return [], ""
    listed = ", ".join(names)
    return images, (
        f"[image {listed} attached at the turn above — still in view here; "
        "describe or re-evaluate it directly.]"
    )


async def carry_forward_content(
    blobs: BlobStore, infos: Sequence[AttachmentInfo], *, image_budget: int
) -> tuple[list[LlmImage], list[str]]:
    """Inline content for images CARRIED FORWARD from earlier turns (not this turn's own
    attachments): re-fetch each persisted blob and wrap it as an LlmImage plus a note flagging
    it as a prior image now back in view — so a vision-capable follow-up ("re-evaluate the
    picture") sees it directly instead of paying an analyze_image round-trip. Capped at
    `image_budget`; a blob that outlived its row is skipped. The bytes ride the CURRENT
    (volatile) user message, so history's cache prefix is untouched — only this turn re-pays.

    FALLBACK path only (the anchored form above is preferred): used when an earlier turn's
    text can't be matched against the client-supplied history, so there is no stable anchor
    position — the image still reaches the model this turn, at the old per-turn re-encode
    cost."""
    images: list[LlmImage] = []
    text_blocks: list[str] = []
    for info in infos:
        if len(images) >= image_budget:
            break
        try:
            data = await blobs.get(info.sha256)
        except FileNotFoundError:
            continue  # row outlived its blob — skip, like build_attachment_content
        images.append(LlmImage(media_type=info.media_type, data=_b64(data)))
        text_blocks.append(
            f'[earlier image "{info.filename}" (id {info.id}) — carried forward from a previous '
            "turn so you can see it again; describe or re-evaluate it directly.]"
        )
    return images, text_blocks


# The client-side decoration a user turn's history entry carries when it attached
# images (frontend/src/agent/useFullBrain.ts `historyContent`). The server mirrors it
# in `decorated_history_text` so an image-attach turn's rendered question is
# byte-identical to the entry the client sends back next turn — a CACHE CONTRACT: any
# drift between the two formats costs a full vision re-encode on the follow-up turn,
# silently. Change both together.
HISTORY_IMAGE_MARKER = "\n\n[Images the owner attached this turn"


def decorated_history_text(raw: str, infos: Sequence[AttachmentInfo]) -> str:
    """The attaching turn's user text exactly as the client's next-turn history entry
    will spell it — the raw message plus the id-reference suffix, in the given
    attachment order (the request's order live; the client mirrors its own array)."""
    refs = "; ".join(f"source_attachment_id={i.id} ({i.filename})" for i in infos)
    return f"{raw}{HISTORY_IMAGE_MARKER} — {refs}]"


@dataclass(frozen=True)
class AttachmentContent:
    """One turn's converted attachments, with the direct image attachments kept
    separable from PDF-page renders so the caller can anchor the images at their
    cache-stable position while the page images stay on the volatile final message."""

    direct_images: list[LlmImage]  # aligned one-to-one with image_infos
    other_images: list[LlmImage]  # PDF page renders
    extra_text: str
    image_infos: list[AttachmentInfo]  # direct image attachments actually included

    @property
    def images(self) -> list[LlmImage]:
        return [*self.direct_images, *self.other_images]


async def build_attachment_content(
    repo: TurnAttachmentRepo,
    blobs: BlobStore,
    ctx: SessionContext,
    attachment_ids: list[str],
    *,
    transcribe_enabled: bool = True,
    can_see_images: bool = False,
) -> AttachmentContent:
    """The turn's attachments converted for the model, in request order.

    Each id is fetched under the session's narrowed firewall (`repo.get(ctx, id)`):
    an out-of-scope or unknown id reads as missing and is SKIPPED (a stray id must
    never crash the turn — the model just doesn't see that file). Bytes come from the
    blob store (CLAUDE.md rule 2). Images and PDF pages share one image budget
    (MAX_IMAGES_PER_TURN); text blocks are joined into one appended section.

    `transcribe_enabled` (the `transcribe` tool is in the turn's registry) shapes the
    audio hint: an actionable tool pointer when on, a plain "not configured" note when
    off, so an audio upload on a whisper-less box is never a dead-end.

    `can_see_images` (the resolved agent.turn model is vision-capable, the SAME gate the
    caller uses to keep image bytes inline) shapes each image note: when the model can see
    the bytes it's told to look directly; when it can't (bytes dropped) the note points at
    analyze_image by id. Defaults False — the safe, text-only wording for any caller that
    can't vouch for vision.
    """
    direct_images: list[LlmImage] = []
    other_images: list[LlmImage] = []
    image_infos: list[AttachmentInfo] = []
    text_blocks: list[str] = []
    for attachment_id in attachment_ids[:MAX_ATTACHMENTS_PER_TURN]:
        info = await repo.get(ctx, attachment_id)
        if info is None:
            continue  # out-of-scope or unknown — invisible to the turn, not an error
        try:
            data = await blobs.get(info.sha256)
        except FileNotFoundError:
            continue  # the row outlived its blob (rare) — skip rather than break the turn
        included = len(direct_images) + len(other_images)
        budget = MAX_IMAGES_PER_TURN - included
        try:
            converted = await asyncio.to_thread(
                _convert_one,
                info,
                data,
                budget,
                transcribe_enabled=transcribe_enabled,
                can_see_images=can_see_images,
            )
        except Exception:
            # A corrupt/encrypted/otherwise-unreadable file must not abort the turn or
            # the other attachments (the docstring's "skipped rather than crashing").
            # Same graceful path as a missing id, plus a minimal note so the model knows
            # something was dropped. SECURITY/robustness: build runs BEFORE the stream's
            # try/finally, so an unhandled raise here 500s the turn and dangles a run-log.
            _log.warning("attachment %s could not be read; skipping", info.id, exc_info=True)
            text_blocks.append(f"[{info.filename}]: could not be read.")
            continue
        kept = converted.images[: MAX_IMAGES_PER_TURN - included]
        if info.media_type.startswith("image/"):
            direct_images.extend(kept)
            if kept:
                image_infos.append(info)
        else:
            other_images.extend(kept)
        text_blocks.extend(converted.text_blocks)
    extra_text = ("\n\n".join(text_blocks)).strip()
    return AttachmentContent(direct_images, other_images, extra_text, image_infos)
