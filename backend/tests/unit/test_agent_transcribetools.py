"""The `transcribe` agent tool: resolve a chat audio attachment by id, delegate to
the faked whisper client, and unload the model after.

Pure unit tests — in-memory attachment repo / blob store / transcribe client, no
LLM, no database. RLS is modeled by membership (an unknown id reads as missing).
"""

import pytest

from jbrain.agent.attachments import AttachmentInfo
from jbrain.agent.loop import ToolContext
from jbrain.agent.transcribetools import build_transcribe_handlers
from jbrain.db.session import SessionContext
from jbrain.transcribe import Transcript

SESSION = "11111111-1111-1111-1111-111111111111"
CTX = ToolContext(
    session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id=SESSION
)


class FakeBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def get(self, sha256: str) -> bytes:
        try:
            return self.data[sha256]
        except KeyError as exc:
            raise FileNotFoundError(sha256) from exc


class FakeAttachments:
    """session_read_context returns a context only for the bound session; get is
    membership-scoped (an unknown id reads as missing, modeling RLS)."""

    def __init__(self) -> None:
        self.rows: dict[str, AttachmentInfo] = {}

    def add(
        self, attachment_id: str, *, media_type: str, sha: str, filename: str, size_bytes: int = 1
    ) -> None:
        self.rows[attachment_id] = AttachmentInfo(
            id=attachment_id,
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha,
            domain_code="general",
        )

    async def session_read_context(
        self, ctx: SessionContext, agent_session_id: str
    ) -> SessionContext | None:
        return ctx if agent_session_id == SESSION else None

    async def get(self, ctx: SessionContext, attachment_id: str) -> AttachmentInfo | None:
        return self.rows.get(attachment_id)


class FakeClient:
    def __init__(self, transcript: Transcript | Exception) -> None:
        self._transcript = transcript
        self.calls: list[dict[str, str]] = []

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        self.calls.append({"filename": filename, "media_type": media_type})
        if isinstance(self._transcript, Exception):
            raise self._transcript
        return self._transcript


class FakeGateway:
    def __init__(self) -> None:
        self.unloaded: list[str] = []

    async def running(self) -> set[str]:
        return set()

    async def load(self, served_model: str) -> None:
        return None

    async def unload(self, served_model: str) -> None:
        self.unloaded.append(served_model)


AUDIO_ID = "22222222-2222-2222-2222-222222222222"


def _tool(
    client: FakeClient,
    blobs: FakeBlobs,
    repo: FakeAttachments,
    gateway: FakeGateway,
    *,
    max_bytes: int = 100 * 1024 * 1024,
):
    return build_transcribe_handlers(
        client,  # type: ignore[arg-type]
        blobs,  # type: ignore[arg-type]
        repo,  # type: ignore[arg-type]
        "whisper-x",
        gateway=gateway,
        max_bytes=max_bytes,
    )["transcribe"]


async def test_transcribes_audio_and_unloads_after() -> None:
    blobs, repo = FakeBlobs(), FakeAttachments()
    blobs.data["sha-a"] = b"RIFF audio"
    repo.add(AUDIO_ID, media_type="audio/wav", sha="sha-a", filename="memo.wav")
    client = FakeClient(Transcript(text="  hello team  "))
    gateway = FakeGateway()

    out = await _tool(client, blobs, repo, gateway)({"source_attachment_id": AUDIO_ID}, CTX)

    assert "memo.wav" in out and "hello team" in out
    assert client.calls == [{"filename": "memo.wav", "media_type": "audio/wav"}]
    assert gateway.unloaded == ["whisper-x"]  # unload-after


async def test_unknown_or_non_uuid_id_is_a_clean_miss() -> None:
    client, gateway = FakeClient(Transcript(text="x")), FakeGateway()
    tool = _tool(client, FakeBlobs(), FakeAttachments(), gateway)
    assert "No attached audio" in await tool({"source_attachment_id": "not-a-uuid"}, CTX)
    assert "No attached audio" in await tool({"source_attachment_id": AUDIO_ID}, CTX)
    assert client.calls == [] and gateway.unloaded == []  # never reached the model


async def test_non_audio_attachment_is_refused() -> None:
    blobs, repo = FakeBlobs(), FakeAttachments()
    blobs.data["sha-i"] = b"png"
    repo.add(AUDIO_ID, media_type="image/png", sha="sha-i", filename="pic.png")
    client = FakeClient(Transcript(text="x"))
    out = await _tool(client, blobs, repo, FakeGateway())({"source_attachment_id": AUDIO_ID}, CTX)
    assert "isn't audio" in out and client.calls == []


async def test_empty_transcript_reports_no_speech() -> None:
    blobs, repo = FakeBlobs(), FakeAttachments()
    blobs.data["sha-a"] = b"RIFF"
    repo.add(AUDIO_ID, media_type="audio/wav", sha="sha-a", filename="silence.wav")
    out = await _tool(FakeClient(Transcript(text="   ")), blobs, repo, FakeGateway())(
        {"source_attachment_id": AUDIO_ID}, CTX
    )
    assert "No speech" in out and "silence.wav" in out


async def test_client_failure_is_a_recoverable_observation_and_still_unloads() -> None:
    blobs, repo = FakeBlobs(), FakeAttachments()
    blobs.data["sha-a"] = b"RIFF"
    repo.add(AUDIO_ID, media_type="audio/wav", sha="sha-a", filename="memo.wav")
    gateway = FakeGateway()
    out = await _tool(FakeClient(RuntimeError("model down")), blobs, repo, gateway)(
        {"source_attachment_id": AUDIO_ID}, CTX
    )
    assert "couldn't transcribe" in out
    assert gateway.unloaded == ["whisper-x"]  # freed even on failure


async def test_no_chat_session_is_a_clean_miss() -> None:
    client = FakeClient(Transcript(text="x"))
    tool = _tool(client, FakeBlobs(), FakeAttachments(), FakeGateway())
    no_session = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())
    assert "No attached audio" in await tool({"source_attachment_id": AUDIO_ID}, no_session)
    assert client.calls == []


async def test_oversized_audio_is_refused_before_the_model() -> None:
    blobs, repo = FakeBlobs(), FakeAttachments()
    blobs.data["sha-a"] = b"RIFF"
    repo.add(AUDIO_ID, media_type="audio/wav", sha="sha-a", filename="huge.wav", size_bytes=2_000)
    client, gateway = FakeClient(Transcript(text="x")), FakeGateway()
    out = await _tool(client, blobs, repo, gateway, max_bytes=1_000)(
        {"source_attachment_id": AUDIO_ID}, CTX
    )
    assert "too large" in out
    assert client.calls == [] and gateway.unloaded == []  # never reached the model


@pytest.mark.parametrize("media_type", ["audio/mpeg", "audio/mp4", "audio/ogg", "audio/flac"])
async def test_common_audio_types_are_accepted(media_type: str) -> None:
    blobs, repo = FakeBlobs(), FakeAttachments()
    blobs.data["sha-a"] = b"data"
    repo.add(AUDIO_ID, media_type=media_type, sha="sha-a", filename="clip")
    out = await _tool(FakeClient(Transcript(text="ok")), blobs, repo, FakeGateway())(
        {"source_attachment_id": AUDIO_ID}, CTX
    )
    assert "ok" in out
