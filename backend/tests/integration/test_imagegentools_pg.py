"""analyze_image handler against real Postgres (no ComfyUI, no network).

The vision read resolves its source BY a prior generated-image id AND BY a chat attachment
id under RLS; a missing / doubled / unknown source is a clean error string with no spend;
and the RapidOCR text-detector gates the second, verbatim vision-OCR pass. Image
generation/editing is not an agent tool — the Images launcher's render path is covered in
tests/integration/test_images_render_pg.py.
"""

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.imagegentools import build_image_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.session import AgentSessionRepo, read_context
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter, resolve_tasks
from jbrain.models.images import GeneratedImageRepo
from jbrain.vision import OcrResult, OcrServiceError
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class MemBlobStore:
    """A minimal in-memory BlobStore for tests: content-addressed put/get. The handler
    only gets; path_for/exists/usage round out the protocol but are unused here."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self._blobs[digest] = data
        return digest

    async def put_stream(self, chunks: AsyncIterator[bytes]) -> str:
        return await self.put(b"".join([chunk async for chunk in chunks]))

    async def get(self, sha256: str) -> bytes:
        try:
            return self._blobs[sha256]
        except KeyError as exc:
            # Match the real BlobStore contract: an absent blob raises FileNotFoundError.
            raise FileNotFoundError(sha256) from exc

    def path_for(self, sha256: str) -> Path:
        return Path(sha256)

    async def exists(self, sha256: str) -> bool:
        return sha256 in self._blobs

    def usage(self) -> tuple[int, int]:
        return len(self._blobs), sum(len(b) for b in self._blobs.values())


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    # The database is module-scoped (one DB per module), so wipe the tables these tests
    # touch before each test — counts/single-row assertions assume a clean slate. The owner
    # holds DELETE on generated_images (immutable, but deletable); attachments cascade off
    # their sessions. Done as the owner under RLS, the same firewall the code runs under.
    await service.rotate_owner_key(SqlAuthRepo(m))
    async with scoped_session(m, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    owner = SessionContext(principal_id=str(pid), principal_kind="owner")
    async with scoped_session(m, owner) as s:
        await s.execute(text("DELETE FROM app.generated_images"))
        await s.execute(text("DELETE FROM app.turn_attachments"))
        await s.execute(text("DELETE FROM app.agent_sessions"))
    yield m
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


def _ctx(owner: SessionContext, session_id: str | None = None) -> ToolContext:
    """A jerv-style tool context: owner identity, EMPTY read scopes (jerv reads no
    knowledge base), carrying the chat session id so analyze_image can widen for
    attachments."""
    return ToolContext(
        session=read_context(owner.principal_id, ()), scopes=(), agent_session_id=session_id
    )


def _router(answer: str = "an analysis of the image") -> LlmRouter:
    """A real router over a canned FakeLlmClient — analyze_image's `agent.vision` call
    routes to the default (xai) client and gets back `answer`. `llm` is exposed on the
    router (`._clients['xai']`) so a test can assert the vision call carried the image."""
    return LlmRouter({"xai": FakeLlmClient(responses=[answer])}, resolve_tasks({}))


class _FakeRapidOcr:
    """A fake RapidOcrClient standing in for the on-box text-DETECTOR analyze_image uses:
    a canned transcription (any non-empty text ⇒ "there is text"), or an OcrServiceError to
    exercise the degrade-to-run-the-OCR-pass-anyway path. Records the bytes it saw."""

    def __init__(self, text: str = "", *, error: bool = False) -> None:
        self._text = text
        self._error = error
        self.calls: list[bytes] = []

    async def ocr(self, data: bytes, media_type: str = "application/octet-stream") -> OcrResult:
        self.calls.append(data)
        if self._error:
            raise OcrServiceError("sidecar down")
        return OcrResult(text=self._text, mean_score=0.9)


def _wired(
    maker: async_sessionmaker,
    router: LlmRouter | None = None,
    rapidocr: _FakeRapidOcr | None = None,
):
    """The handler dict plus the seams a test seeds through: (handlers, blobs, repo,
    sessions, attachments)."""
    sessions = AgentSessionRepo(maker)
    attachments = TurnAttachmentRepo(maker, sessions)
    blobs = MemBlobStore()
    repo = GeneratedImageRepo()
    handlers = build_image_handlers(
        blobs,
        repo,
        attachments,
        maker,
        router or _router(),
        rapidocr=rapidocr,
    )
    return handlers, blobs, repo, sessions, attachments


async def _seed_generated_image(
    maker: async_sessionmaker,
    owner: SessionContext,
    blobs: MemBlobStore,
    repo: GeneratedImageRepo,
    data: bytes = b"\x89PNG\r\n\x1a\nsource-bytes",
) -> str:
    """Insert one generated-image row + blob directly (the launcher's write path, faked) —
    a resolvable analyze_image source id."""
    sha = await blobs.put(data)
    async with scoped_session(maker, owner) as session:
        row = await repo.insert(
            session,
            blob_sha256=sha,
            kind="generate",
            model="qwen-image-2512",
            prompt="a seeded source",
            source_sha256=None,
            width=1024,
            height=1024,
            steps=20,
            seed=7,
        )
        return str(row.id)


async def test_analyze_by_generated_id_returns_vision_answer(maker: async_sessionmaker) -> None:
    """analyze_image resolves a prior generated image by id, sends its bytes to the vision
    route, and returns the model's text — read-only: a plain string, no view, no new row."""
    owner = await _owner(maker)
    router = _router("a red bicycle leaning on a wall")
    handlers, blobs, repo, _, _ = _wired(maker, router=router)
    source_id = await _seed_generated_image(maker, owner, blobs, repo)

    out = await handlers["analyze_image"](
        {"prompt": "what is in this image?", "source_image_id": source_id}, _ctx(owner)
    )
    assert out == "a red bicycle leaning on a wall"
    assert not isinstance(out, ToolOutput)  # a read — no inline view
    # The vision route saw the question and the image bytes (one LlmImage).
    call = router._clients["xai"].calls[0]  # type: ignore[attr-defined]
    assert call["user_text"] == "what is in this image?"
    assert len(call["images"]) == 1

    async with scoped_session(maker, owner) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.generated_images"))).scalar()
    assert count == 1  # only the seeded row — analyze inserts nothing


async def test_analyze_by_attachment_id_returns_vision_answer(maker: async_sessionmaker) -> None:
    """analyze_image reads a chat attachment by id under the widened attachment context —
    the RLS path shared with the compare tool — and answers from its bytes."""
    owner = await _owner(maker)
    handlers, blobs, _, sessions, attachments = _wired(
        maker, router=_router("a sign that reads OPEN")
    )

    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    att_ctx = await attachments.session_read_context(owner, info.id)
    assert att_ctx is not None
    sha = await blobs.put(b"\x89PNG\r\n\x1a\nattached-bytes")
    att = await attachments.add(
        att_ctx,
        info.id,
        sha256=sha,
        filename="photo.png",
        media_type="image/png",
        size_bytes=10,
        domain_code="general",
    )

    out = await handlers["analyze_image"](
        {"prompt": "read the sign", "source_attachment_id": att.id}, _ctx(owner, info.id)
    )
    assert out == "a sign that reads OPEN"


async def test_analyze_orphan_source_blob_is_clean_error(maker: async_sessionmaker) -> None:
    """A generated row that outlives its blob (orphan) must yield a clean tool-error string,
    never a raw FileNotFoundError exposing the blob path to the model."""
    owner = await _owner(maker)
    router = _router()
    handlers, blobs, repo, _, _ = _wired(maker, router=router)
    source_id = await _seed_generated_image(maker, owner, blobs, repo)
    blobs._blobs.clear()  # evict the blob; the row now points at nothing

    out = await handlers["analyze_image"](
        {"prompt": "describe it", "source_image_id": source_id}, _ctx(owner)
    )
    assert isinstance(out, str) and not isinstance(out, ToolOutput)
    assert "no longer available" in out.lower()  # clean message, no path/stack leaked
    assert router._clients["xai"].calls == []  # type: ignore[attr-defined] - never spent


async def test_analyze_needs_a_prompt_and_one_source(maker: async_sessionmaker) -> None:
    """A missing prompt or a bad source (neither/both) is a clean error string naming
    analyze_image, and never reaches the vision model."""
    owner = await _owner(maker)
    router = _router()
    handlers, _, _, _, _ = _wired(maker, router=router)

    no_prompt = await handlers["analyze_image"]({"source_image_id": "x"}, _ctx(owner))
    assert isinstance(no_prompt, str) and "prompt" in no_prompt.lower()

    both = await handlers["analyze_image"](
        {"prompt": "what is this", "source_image_id": "a", "source_attachment_id": "b"},
        _ctx(owner),
    )
    assert isinstance(both, str) and "analyze_image" in both

    neither = await handlers["analyze_image"]({"prompt": "what is this"}, _ctx(owner))
    assert isinstance(neither, str) and "analyze_image" in neither

    assert router._clients["xai"].calls == []  # type: ignore[attr-defined] - never spent


async def test_non_uuid_source_id_is_a_clean_miss_not_a_db_error(maker: async_sessionmaker) -> None:
    """A model guessing a non-uuid source id ("latest") under a REAL session must read as a
    clean miss, never a raw DB DataError."""
    owner = await _owner(maker)
    sessions = AgentSessionRepo(maker)
    info = await sessions.create(owner, domain_scopes=(), agent="jerv")
    router = _router()
    handlers, _, _, _, _ = _wired(maker, router=router)
    ctx = _ctx(owner, info.id)  # a real session — the path that reaches the attachment query

    analyzed = await handlers["analyze_image"](
        {"prompt": "is the person female?", "source_attachment_id": "latest"}, ctx
    )
    assert analyzed == "No attached image with that id is in this chat."
    assert router._clients["xai"].calls == []  # type: ignore[attr-defined] - never spent

    # A non-uuid generated id is the same clean miss (no DB argument error).
    bad_gen = await handlers["analyze_image"]({"prompt": "describe", "source_image_id": "x"}, ctx)
    assert bad_gen == "No generated image with that id is in this chat."


async def test_unknown_generated_id_is_a_clean_miss(maker: async_sessionmaker) -> None:
    """A well-formed uuid that matches no row (or an out-of-scope one RLS hides) reads as
    the same clean miss — the model never learns whether the row exists."""
    owner = await _owner(maker)
    router = _router()
    handlers, _, _, _, _ = _wired(maker, router=router)

    out = await handlers["analyze_image"](
        {"prompt": "describe", "source_image_id": "00000000-0000-0000-0000-000000000000"},
        _ctx(owner),
    )
    assert out == "No generated image with that id is in this chat."
    assert router._clients["xai"].calls == []  # type: ignore[attr-defined] - never spent


async def test_analyze_appends_verbatim_markdown_when_text_detected(
    maker: async_sessionmaker,
) -> None:
    """When RapidOCR detects text, analyze_image runs the second vision pass and appends its
    verbatim Markdown transcription under a heading — description + exact text in one call. The
    OCR pass carries the Markdown system prompt and the wide OCR token budget."""
    from jbrain.ingest.ocr import OCR_MAX_TOKENS

    owner = await _owner(maker)
    router = LlmRouter(
        {"xai": FakeLlmClient(["A one-page offering document.", "# Offering\n\n**Adopted** 2024"])},
        resolve_tasks({}),
    )
    rapid = _FakeRapidOcr("offering document 2024")  # non-empty ⇒ "there is text"
    handlers, blobs, repo, _, _ = _wired(maker, router=router, rapidocr=rapid)
    source_id = await _seed_generated_image(maker, owner, blobs, repo)

    out = await handlers["analyze_image"](
        {"prompt": "what is this document?", "source_image_id": source_id}, _ctx(owner)
    )

    assert out == (
        "A one-page offering document.\n\n"
        "--- Full text (verbatim) ---\n"
        "# Offering\n\n**Adopted** 2024"
    )
    calls = router._clients["xai"].calls  # type: ignore[attr-defined]
    assert len(calls) == 2  # describe pass, then the verbatim-OCR pass
    assert calls[0]["user_text"] == "what is this document?"  # the describe pass
    assert "markdown" in calls[1]["system"].lower()  # the OCR pass carries the Markdown prompt
    assert calls[1]["max_tokens"] == OCR_MAX_TOKENS  # …at the wide transcription budget
    assert len(rapid.calls) == 1  # the detector ran once, on the image bytes


async def test_analyze_skips_ocr_pass_when_no_text_detected(maker: async_sessionmaker) -> None:
    """A text-less image (RapidOCR finds nothing) gets the description only — the second vision
    pass never runs, so a photo doesn't pay for a transcription it has no text to produce."""
    owner = await _owner(maker)
    router = LlmRouter(
        {"xai": FakeLlmClient(["A golden retriever on a beach."])}, resolve_tasks({})
    )
    rapid = _FakeRapidOcr("")  # no legible text
    handlers, blobs, repo, _, _ = _wired(maker, router=router, rapidocr=rapid)
    source_id = await _seed_generated_image(maker, owner, blobs, repo)

    out = await handlers["analyze_image"](
        {"prompt": "what is this?", "source_image_id": source_id}, _ctx(owner)
    )

    assert out == "A golden retriever on a beach."  # description only, no verbatim block
    assert len(router._clients["xai"].calls) == 1  # type: ignore[attr-defined] - OCR pass skipped
    assert len(rapid.calls) == 1  # the detector still ran


async def test_analyze_without_rapidocr_stays_description_only(maker: async_sessionmaker) -> None:
    """No detector wired (rapidocr=None) ⇒ analyze_image is the pre-existing description-only
    read — the OCR augmentation needs the gate, and jerv still has the dedicated `ocr` tool."""
    owner = await _owner(maker)
    router = LlmRouter({"xai": FakeLlmClient(["Just a description."])}, resolve_tasks({}))
    handlers, blobs, repo, _, _ = _wired(maker, router=router)  # rapidocr=None
    source_id = await _seed_generated_image(maker, owner, blobs, repo)

    out = await handlers["analyze_image"](
        {"prompt": "describe it", "source_image_id": source_id}, _ctx(owner)
    )

    assert out == "Just a description."
    assert len(router._clients["xai"].calls) == 1  # type: ignore[attr-defined] - no second pass


async def test_analyze_runs_ocr_pass_when_detector_unavailable(maker: async_sessionmaker) -> None:
    """A wired-but-unreachable detector degrades to running the OCR pass anyway — the Markdown
    prompt self-gates (empty on a text-less image), so a transient sidecar outage never hides
    text that is there."""
    owner = await _owner(maker)
    router = LlmRouter(
        {"xai": FakeLlmClient(["A form.", "## Form\n\n| a | b |\n|---|---|"])}, resolve_tasks({})
    )
    rapid = _FakeRapidOcr(error=True)  # sidecar down ⇒ OcrServiceError
    handlers, blobs, repo, _, _ = _wired(maker, router=router, rapidocr=rapid)
    source_id = await _seed_generated_image(maker, owner, blobs, repo)

    out = await handlers["analyze_image"](
        {"prompt": "read it", "source_image_id": source_id}, _ctx(owner)
    )

    assert out == "A form.\n\n--- Full text (verbatim) ---\n## Form\n\n| a | b |\n|---|---|"
    assert len(router._clients["xai"].calls) == 2  # type: ignore[attr-defined] - OCR pass ran
