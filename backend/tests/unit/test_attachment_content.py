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


async def _build(repo: FakeRepo, blobs: FakeBlobs, ids: list[str]) -> tuple[list, str]:
    return await build_attachment_content(repo, blobs, CTX, ids)  # type: ignore[arg-type]


async def test_image_becomes_one_llm_image() -> None:
    repo, blobs = FakeRepo(), FakeBlobs()
    blobs.put("sha-img", b"\x89PNG-bytes")
    aid = repo.add("image/png", "sha-img", filename="scan.png")
    images, text = await _build(repo, blobs, [aid])
    assert text == ""
    assert len(images) == 1
    assert images[0].media_type == "image/png"
    assert base64.b64decode(images[0].data) == b"\x89PNG-bytes"


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
