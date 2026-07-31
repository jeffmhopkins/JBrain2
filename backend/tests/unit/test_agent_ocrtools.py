"""The jerv `ocr` tool: resolve a chat image/PDF attachment by id and return RapidOCR's
verbatim transcription over faked services (docs/plans/RAPIDOCR_PLAN.md). Pure unit tests —
in-memory attachment repo / blob store, a fake sidecar. RLS is modeled by membership."""

import pymupdf

from jbrain.agent.attachments import AttachmentInfo
from jbrain.agent.loop import ToolContext
from jbrain.agent.ocrtools import build_ocr_handlers
from jbrain.db.session import SessionContext
from jbrain.vision import OcrResult, OcrServiceError

SESSION = "11111111-1111-1111-1111-111111111111"
ATT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CTX = ToolContext(
    session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id=SESSION
)


class FakeBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        key = f"sha-{len(self.data)}"
        self.data[key] = data
        return key

    async def get(self, sha256: str) -> bytes:
        try:
            return self.data[sha256]
        except KeyError as exc:
            raise FileNotFoundError(sha256) from exc


class FakeAttachments:
    def __init__(self) -> None:
        self.rows: dict[str, AttachmentInfo] = {}

    def add(self, attachment_id: str, *, media_type: str, sha: str, filename: str) -> None:
        self.rows[attachment_id] = AttachmentInfo(
            id=attachment_id,
            filename=filename,
            media_type=media_type,
            size_bytes=10,
            sha256=sha,
            domain_code="general",
        )

    async def session_read_context(
        self, ctx: SessionContext, agent_session_id: str
    ) -> SessionContext | None:
        return ctx if agent_session_id == SESSION else None

    async def get(self, ctx: SessionContext, attachment_id: str) -> AttachmentInfo | None:
        return self.rows.get(attachment_id)


class FakeRapid:
    def __init__(self, *results: OcrResult, error: bool = False) -> None:
        self._results = list(results)
        self._error = error
        self.calls = 0

    async def ocr(self, data: bytes, media_type: str = "application/octet-stream") -> OcrResult:
        self.calls += 1
        if self._error:
            raise OcrServiceError("sidecar down")
        # One result per call (PDF pages consume in order); reuse the last if exhausted.
        return self._results[min(self.calls - 1, len(self._results) - 1)]


def _tool(rapid: object, blobs: object, atts: object):  # type: ignore[no-untyped-def]
    return build_ocr_handlers(rapid, blobs, atts)["ocr"]  # type: ignore[arg-type]


async def test_reads_text_from_an_image_attachment() -> None:
    blobs, atts = FakeBlobs(), FakeAttachments()
    sha = await blobs.put(b"\x89PNGdata")
    atts.add(ATT, media_type="image/png", sha=sha, filename="error.png")
    rapid = FakeRapid(OcrResult(text="Traceback (most recent call last)", mean_score=0.97))
    out = await _tool(rapid, blobs, atts)({"source_attachment_id": ATT}, CTX)
    assert "Traceback (most recent call last)" in out
    assert "error.png" in out
    assert rapid.calls == 1


async def test_unknown_attachment_is_a_clean_miss() -> None:
    out = await _tool(FakeRapid(), FakeBlobs(), FakeAttachments())(
        {"source_attachment_id": ATT}, CTX
    )
    assert out == "No attached image or PDF with that id is in this chat."


async def test_foreign_session_is_a_clean_miss() -> None:
    blobs, atts = FakeBlobs(), FakeAttachments()
    sha = await blobs.put(b"x")
    atts.add(ATT, media_type="image/png", sha=sha, filename="a.png")
    other = ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        agent_session_id="99999999-9999-9999-9999-999999999999",
    )
    out = await _tool(FakeRapid(), blobs, atts)({"source_attachment_id": ATT}, other)
    assert out == "No attached image or PDF with that id is in this chat."


async def test_non_image_attachment_is_refused() -> None:
    blobs, atts = FakeBlobs(), FakeAttachments()
    sha = await blobs.put(b"stuff")
    atts.add(ATT, media_type="text/plain", sha=sha, filename="notes.txt")
    out = await _tool(FakeRapid(), blobs, atts)({"source_attachment_id": ATT}, CTX)
    assert "reads images and PDFs" in out


async def test_service_error_degrades_to_a_clear_message() -> None:
    blobs, atts = FakeBlobs(), FakeAttachments()
    sha = await blobs.put(b"img")
    atts.add(ATT, media_type="image/png", sha=sha, filename="a.png")
    out = await _tool(FakeRapid(error=True), blobs, atts)({"source_attachment_id": ATT}, CTX)
    assert out == "The on-box OCR service isn't available right now."


async def test_empty_transcription_reports_no_text() -> None:
    blobs, atts = FakeBlobs(), FakeAttachments()
    sha = await blobs.put(b"blur")
    atts.add(ATT, media_type="image/png", sha=sha, filename="blur.png")
    rapid = FakeRapid(OcrResult(text="   ", mean_score=0.0))
    out = await _tool(rapid, blobs, atts)({"source_attachment_id": ATT}, CTX)
    assert out == "No legible text was found in that file."


async def test_reads_a_pdf_page_by_page() -> None:
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    pdf_bytes = doc.tobytes()
    blobs, atts = FakeBlobs(), FakeAttachments()
    sha = await blobs.put(pdf_bytes)
    atts.add(ATT, media_type="application/pdf", sha=sha, filename="scan.pdf")
    rapid = FakeRapid(OcrResult("page one text", 0.9), OcrResult("page two text", 0.9))
    out = await _tool(rapid, blobs, atts)({"source_attachment_id": ATT}, CTX)
    assert "[page 1]" in out and "page one text" in out
    assert "[page 2]" in out and "page two text" in out
    assert rapid.calls == 2
