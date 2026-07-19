"""`grab_frame` against real Postgres (migrations 0077 + 0139): the handler grabs a
still (URL or attachment — media faked), persists it as a `provenance='ffmpeg'`
generated_images row, returns the `generated_image` card (suppressed by `show=false`),
and — with a `question` — runs the vision read inline. Error paths (one source, a live
stream, no frame, a non-video attachment) are covered too.

Media (resolve + sample) and the vision model are faked; the persistence + provenance +
RLS are exercised for real, since that is the part real Postgres validates."""

import io
from collections.abc import AsyncIterator
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.attachments import AttachmentInfo
from jbrain.agent.grabtools import build_grab_frame_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.media import SampledFrame
from jbrain.models.images import GeneratedImageRepo
from jbrain.stream import ResolvedStream, StreamError, StreamSample
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SESSION = "11111111-1111-1111-1111-111111111111"
ATT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _jpeg(w: int = 64, h: int = 48, color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return buf.getvalue()


class FakeBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        key = f"sha-{len(self.data)}-{len(data)}"
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

    def add(self, attachment_id: str, *, media_type: str, sha: str) -> None:
        self.rows[attachment_id] = AttachmentInfo(
            id=attachment_id,
            filename="clip.mp4",
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


VOD = ResolvedStream(
    media_url="https://cdn.example.com/v.mp4",
    title="Modular Jam",
    is_live=False,
    duration_s=600.0,
    webpage_url="https://youtube.com/watch?v=abc",
)
LIVE = ResolvedStream(
    media_url="https://cdn.example.com/live.m3u8",
    title="Live Cam",
    is_live=True,
    duration_s=None,
    webpage_url="https://youtube.com/live/xyz",
)


def _resolver(resolved: ResolvedStream):
    def resolve(url: str, *, max_height: int = 720, skip_guard: bool = False) -> ResolvedStream:
        return resolved

    return resolve


def _raising_resolver():
    def resolve(url: str, *, max_height: int = 720, skip_guard: bool = False) -> ResolvedStream:
        raise StreamError("that URL couldn't be opened as a video stream")

    return resolve


def _sampler(sample: StreamSample):
    async def sample_stream(resolved: ResolvedStream, **kw) -> StreamSample:
        return sample

    return sample_stream


def _router(fake: FakeLlmClient) -> LlmRouter:
    return LlmRouter({"xai": fake}, {"agent.vision": ("xai", "grok-4.3")})


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


def _handlers(
    maker: async_sessionmaker,
    *,
    resolved: ResolvedStream = VOD,
    frame: bytes | None = None,
    fake: FakeLlmClient | None = None,
    blobs: FakeBlobs | None = None,
    attachments: FakeAttachments | None = None,
    raising_resolver: bool = False,
):
    sample = StreamSample(frames=[SampledFrame(0, frame)] if frame is not None else [])
    # The fakes stand in for BlobStore / TurnAttachmentRepo (the repo test pattern); typed
    # Any at the seam so pyright accepts the structural stand-ins.
    b: Any = blobs or FakeBlobs()
    a: Any = attachments or FakeAttachments()
    return build_grab_frame_handlers(
        b,
        a,
        GeneratedImageRepo(),
        maker,
        _router(fake or FakeLlmClient([])),
        resolver=_raising_resolver() if raising_resolver else _resolver(resolved),
        sampler=_sampler(sample),
    )


def _ctx(owner: SessionContext) -> ToolContext:
    return ToolContext(session=owner, scopes=(), agent_session_id=SESSION)


async def test_url_grab_persists_provenanced_row(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    handlers = _handlers(maker, frame=_jpeg(64, 48))

    out = await handlers["grab_frame"]({"url": "u", "seek": 164}, _ctx(owner))

    assert isinstance(out, ToolOutput) and out.view is not None
    data = out.view.data
    assert data["provenance"] == "ffmpeg" and data["kind"] == "generate"
    assert data["width"] == 64 and data["height"] == 48
    image_id = str(data["image_id"])
    assert f"source_image_id {image_id}" in out  # the model is told how to re-use it

    # The row really landed, stamped ffmpeg, resolvable by id, hidden from the gallery.
    async with scoped_session(maker, owner) as session:
        row = await GeneratedImageRepo().get(session, image_id)
        gallery = await GeneratedImageRepo().list(session, limit=1000)
    assert row is not None and row.provenance == "ffmpeg" and row.width == 64
    assert image_id not in {str(r.id) for r in gallery}


async def test_show_false_suppresses_the_card(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    handlers = _handlers(maker, frame=_jpeg())

    out = await handlers["grab_frame"]({"url": "u", "seek": 5, "show": False}, _ctx(owner))

    assert isinstance(out, ToolOutput) and out.view is None  # no card, but still an image_id
    assert "image_id" in out


async def test_question_runs_inline_vision_read(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    fake = FakeLlmClient(["An Intellijel Metropolis sequencer, silver panel."])
    handlers = _handlers(maker, frame=_jpeg(), fake=fake)

    out = await handlers["grab_frame"](
        {"url": "u", "seek": 164, "question": "what module is this?"}, _ctx(owner)
    )

    assert isinstance(out, ToolOutput)
    assert "What it shows:" in out and "Metropolis" in out


async def test_attachment_grab_persists_from_attachment_bytes(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    blobs = FakeBlobs()
    sha = await blobs.put(b"fake mp4 bytes")
    atts = FakeAttachments()
    atts.add(ATT, media_type="video/mp4", sha=sha)
    handlers = _handlers(maker, frame=_jpeg(80, 60), blobs=blobs, attachments=atts)

    out = await handlers["grab_frame"]({"source_attachment_id": ATT, "seek": 3}, _ctx(owner))

    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["provenance"] == "ffmpeg" and out.view.data["width"] == 80


async def test_one_source_rule_and_error_paths(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)

    # Both or neither source → a clean rule message, nothing stored.
    h = _handlers(maker, frame=_jpeg())
    both = await h["grab_frame"]({"url": "u", "source_attachment_id": ATT}, _ctx(owner))
    neither = await h["grab_frame"]({"seek": 1}, _ctx(owner))
    assert "exactly one source" in both and "exactly one source" in neither

    # A live stream has no fixed timestamp — refused, not grabbed.
    live = _handlers(maker, resolved=LIVE, frame=_jpeg())
    assert "live stream" in (await live["grab_frame"]({"url": "u", "seek": 5}, _ctx(owner)))

    # A resolve failure surfaces as a clean tool error.
    bad = _handlers(maker, raising_resolver=True)
    assert "couldn't be opened" in (await bad["grab_frame"]({"url": "u"}, _ctx(owner)))

    # No decodable frame → a clean "couldn't read a frame" message.
    empty = _handlers(maker, frame=None)
    no_frame = await empty["grab_frame"]({"url": "u", "seek": 9}, _ctx(owner))
    assert "couldn't read a frame" in no_frame

    # A foreign / non-video attachment reads as a clean miss / refusal.
    atts = FakeAttachments()
    atts.add(ATT, media_type="image/png", sha="x")
    notvideo = _handlers(maker, attachments=atts, frame=_jpeg())
    assert "isn't a video" in (
        await notvideo["grab_frame"]({"source_attachment_id": ATT}, _ctx(owner))
    )
    miss = _handlers(maker, frame=_jpeg())
    assert "No attached video" in (
        await miss["grab_frame"]({"source_attachment_id": ATT}, _ctx(owner))
    )
