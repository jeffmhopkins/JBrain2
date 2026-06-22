"""Attachment → LLM-content conversion (chat attachments, Stage-2 Wave 2).

Pure unit tests with an in-memory attachment repo and blob store: an image becomes
one LlmImage; a PDF becomes per-page images + extracted text; a text file becomes a
labeled text block; a missing/out-of-scope id is skipped (never a crash); the caps
hold. No LLM, no database.
"""

import base64

import pymupdf

from jbrain.agent.attachment_content import (
    MAX_ATTACHMENTS_PER_TURN,
    MAX_IMAGES_PER_TURN,
    MAX_PDF_PAGE_PIXELS,
    MAX_PDF_PAGES,
    build_attachment_content,
)
from jbrain.agent.attachments import AttachmentInfo
from jbrain.db.session import SessionContext

CTX = SessionContext(principal_id="p", principal_kind="owner")


class FakeBlobs:
    """A content-addressed store keyed by the sha256 the repo records."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put(self, sha: str, data: bytes) -> None:
        self.data[sha] = data

    async def get(self, sha256: str) -> bytes:
        try:
            return self.data[sha256]
        except KeyError as exc:
            raise FileNotFoundError(sha256) from exc


class FakeRepo:
    """Just `get`, RLS modeled by membership: an unknown id reads as missing."""

    def __init__(self) -> None:
        self.rows: dict[str, AttachmentInfo] = {}

    def add(self, media_type: str, sha: str, *, filename: str = "f") -> str:
        info = AttachmentInfo(
            id=f"a{len(self.rows) + 1}",
            filename=filename,
            media_type=media_type,
            size_bytes=1,
            sha256=sha,
            domain_code="general",
        )
        self.rows[info.id] = info
        return info.id

    async def get(self, ctx: SessionContext, attachment_id: str) -> AttachmentInfo | None:
        return self.rows.get(attachment_id)


def make_pdf(*page_texts: str | None) -> bytes:
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        if text is not None:
            page.insert_text((72, 72), text)
    return doc.tobytes()


def make_oversized_pdf(text: str = "tiny but huge") -> bytes:
    """A small PDF whose single page declares an enormous MediaBox — the rasterization
    DoS vector: a few bytes that render to a multi-GB pixmap at the base zoom."""
    doc = pymupdf.open()
    page = doc.new_page(width=14400, height=14400)
    page.insert_text((72, 72), text)
    return doc.tobytes()


def make_encrypted_pdf(text: str = "secret") -> bytes:
    """A real password-protected PDF — load_page/get_text raise on it without the key."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,  # type: ignore[attr-defined]
        owner_pw="owner-secret",
        user_pw="user-secret",
    )


def _png_dimensions(png: bytes) -> tuple[int, int]:
    """The decoded pixel (width, height) of a PNG — to assert the rendered pixmap was
    bounded rather than trusting the byte length."""
    with pymupdf.open(stream=png, filetype="png") as doc:
        rect = doc.load_page(0).rect
        return int(rect.width), int(rect.height)


async def _build(repo: FakeRepo, blobs: FakeBlobs, ids: list[str]) -> tuple[list, str]:
    return await build_attachment_content(repo, blobs, CTX, ids)  # type: ignore[arg-type]


async def test_audio_becomes_a_transcribe_hint_not_inline_bytes() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    # Binary audio must never be decoded as text (garbage); it surfaces only its id.
    blobs.put("sha-aud", b"RIFF\x00\x01\x02binary-audio")
    aid = repo.add("audio/wav", "sha-aud", filename="memo.wav")
    images, text = await _build(repo, blobs, [aid])
    assert images == []
    assert aid in text
    assert "source_attachment_id" in text and "transcribe" in text
    assert "memo.wav" in text
    assert "binary-audio" not in text  # the bytes are not decoded inline


async def test_audio_hint_says_not_configured_when_transcription_is_off() -> None:
    """No dead-end: with the whisper backend off, the hint doesn't point at a tool
    that was dropped from the registry."""
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-aud", b"RIFF audio")
    aid = repo.add("audio/wav", "sha-aud", filename="memo.wav")
    images, text = await build_attachment_content(
        repo,  # type: ignore[arg-type]
        blobs,  # type: ignore[arg-type]
        CTX,
        [aid],
        transcribe_enabled=False,
    )
    assert images == []
    assert "memo.wav" in text and "not configured" in text
    assert "source_attachment_id" not in text  # never points at the missing tool


async def test_video_becomes_a_transcribe_hint_not_inline_bytes() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    # A video is unreadable inline like audio, but transcribable (the gateway pulls
    # its audio track) — it surfaces its id pointing at the transcribe tool.
    blobs.put("sha-vid", b"\x00\x00\x00\x18ftypmp42binary-video")
    aid = repo.add("video/mp4", "sha-vid", filename="clip.mp4")
    images, text = await _build(repo, blobs, [aid])
    assert images == []
    assert aid in text
    assert "video" in text and "source_attachment_id" in text and "transcribe" in text
    assert "binary-video" not in text  # the bytes are not decoded inline


async def test_image_becomes_one_llm_image() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-img", b"\x89PNG-bytes")
    aid = repo.add("image/png", "sha-img", filename="scan.png")
    images, text = await _build(repo, blobs, [aid])
    assert len(images) == 1
    assert images[0].media_type == "image/png"
    assert base64.b64decode(images[0].data) == b"\x89PNG-bytes"
    # The image's id is named in the text so the model can act on it by reference
    # (edit or analyze) even when it can't see the bytes.
    assert aid in text
    assert "source_attachment_id" in text
    assert "analyze_image" in text and "edit_image" in text
    assert "scan.png" in text


async def test_pdf_yields_page_images_and_extracted_text() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    pdf = make_pdf("page one text", "page two text")
    blobs.put("sha-pdf", pdf)
    aid = repo.add("application/pdf", "sha-pdf", filename="report.pdf")
    images, text = await _build(repo, blobs, [aid])
    # One PNG per page for vision…
    assert len(images) == 2
    assert all(im.media_type == "image/png" for im in images)
    # …and each page's text layer, labeled with filename + page number.
    assert "[report.pdf, page 1]:" in text and "page one text" in text
    assert "[report.pdf, page 2]:" in text and "page two text" in text


async def test_text_file_becomes_a_labeled_text_block() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-txt", b"hello\nworld")
    aid = repo.add("text/plain", "sha-txt", filename="notes.txt")
    images, text = await _build(repo, blobs, [aid])
    assert images == []
    assert text == "[notes.txt]:\nhello\nworld"


async def test_json_and_csv_decode_as_text() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-json", b'{"k": 1}')
    blobs.put("sha-csv", b"a,b\n1,2")
    jid = repo.add("application/json", "sha-json", filename="d.json")
    cid = repo.add("text/csv", "sha-csv", filename="d.csv")
    _images, text = await _build(repo, blobs, [jid, cid])
    assert '[d.json]:\n{"k": 1}' in text
    assert "[d.csv]:\na,b\n1,2" in text


async def test_missing_or_out_of_scope_id_is_skipped_not_crashed() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-img", b"img")
    aid = repo.add("image/png", "sha-img")
    # A stray id (unknown to the repo, like an out-of-scope row) is invisible: the
    # turn still gets the one good attachment, no exception.
    images, _text = await _build(repo, blobs, ["does-not-exist", aid])
    assert len(images) == 1


async def test_row_without_its_blob_is_skipped() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    aid = repo.add("image/png", "sha-gone")  # row exists, blob never stored
    images, text = await _build(repo, blobs, [aid])
    assert images == [] and text == ""


async def test_attachment_count_cap_truncates() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    ids = []
    for i in range(MAX_ATTACHMENTS_PER_TURN + 5):
        blobs.put(f"sha{i}", b"x")
        ids.append(repo.add("image/png", f"sha{i}"))
    images, _text = await _build(repo, blobs, ids)
    # Only the first MAX_ATTACHMENTS_PER_TURN ids are processed.
    assert len(images) == MAX_ATTACHMENTS_PER_TURN


async def test_image_cap_holds_across_pdf_pages() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    # Two PDFs whose page count together exceeds the image cap; pages are within the
    # per-PDF page cap each but the OVERALL image budget stops the overflow.
    pages = [f"p{i}" for i in range(MAX_PDF_PAGES)]
    blobs.put("pdf-a", make_pdf(*pages))
    blobs.put("pdf-b", make_pdf(*pages))
    a = repo.add("application/pdf", "pdf-a", filename="a.pdf")
    b = repo.add("application/pdf", "pdf-b", filename="b.pdf")
    images, _text = await _build(repo, blobs, [a, b])
    assert len(images) <= MAX_IMAGES_PER_TURN


async def test_pdf_page_cap_truncates_and_notes_it() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    extra = MAX_PDF_PAGES + 3
    blobs.put("big", make_pdf(*[f"page {i}" for i in range(extra)]))
    aid = repo.add("application/pdf", "big", filename="big.pdf")
    images, text = await _build(repo, blobs, [aid])
    # At most MAX_PDF_PAGES rendered images, with a truncation note in the text.
    assert len(images) == MAX_PDF_PAGES
    assert f"first {MAX_PDF_PAGES} of {extra} pages" in text


async def test_empty_attachment_ids_returns_empty() -> None:
    images, text = await _build(FakeRepo(), FakeBlobs(), [])
    assert images == [] and text == ""


async def test_oversized_mediabox_pdf_is_bounded() -> None:
    # A small PDF with a 14400x14400-point MediaBox would render to a multi-GB pixmap
    # at the base zoom; the per-page zoom floor keeps the decoded image within the
    # pixel budget, while the page text still comes through.
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-big-box", make_oversized_pdf("tiny but huge"))
    aid = repo.add("application/pdf", "sha-big-box", filename="bomb.pdf")
    images, text = await _build(repo, blobs, [aid])
    assert len(images) == 1
    width, height = _png_dimensions(base64.b64decode(images[0].data))
    assert width * height <= MAX_PDF_PAGE_PIXELS
    assert "tiny but huge" in text


async def test_corrupt_pdf_is_skipped_not_crashed() -> None:
    # Garbage bytes labeled application/pdf make pymupdf.open raise; the bad file is
    # omitted while another valid attachment in the same call still converts — no crash.
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-garbage", b"not a real pdf at all \x00\x01\x02")
    blobs.put("sha-ok", b"\x89PNG-ok")
    bad = repo.add("application/pdf", "sha-garbage", filename="broken.pdf")
    good = repo.add("image/png", "sha-ok", filename="ok.png")
    images, _text = await _build(repo, blobs, [bad, good])
    # The corrupt PDF produced no images; the good image still made it through.
    assert len(images) == 1
    assert base64.b64decode(images[0].data) == b"\x89PNG-ok"


async def test_encrypted_pdf_is_skipped_not_crashed() -> None:
    # A password-protected PDF raises on page load/extract; it is skipped, and a valid
    # sibling attachment in the same call still converts.
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-enc", make_encrypted_pdf("secret"))
    blobs.put("sha-txt", b"plain text survives")
    bad = repo.add("application/pdf", "sha-enc", filename="locked.pdf")
    good = repo.add("text/plain", "sha-txt", filename="notes.txt")
    images, text = await _build(repo, blobs, [bad, good])
    assert images == []  # nothing rendered from the encrypted file
    assert "plain text survives" in text  # the sibling text file still converted
