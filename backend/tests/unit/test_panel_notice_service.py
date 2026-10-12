"""The notice service's parts that need no database: the sound, and Jerv's tool over it.

The send path, the refusals at the door and the panel's routes run against real Postgres in
`tests/integration/test_panel_notice_api_pg.py`; here are the decode and speech branches a
route test would have to fake a whole box to reach, and `panel_notify`'s handler over a
stand-in service.
"""

from __future__ import annotations

import shutil
from typing import Any, cast

import httpx
import pytest

from jbrain.agent import paneltools
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.jpanel import notice as N
from jbrain.jpanel import notice_service as svc_mod
from jbrain.jpanel.notice_service import NoticeService, NoticeView, Sent
from tests.unit.test_panel_notice import tone, wav

pytestmark = pytest.mark.asyncio


def service(**kw: Any) -> NoticeService:
    return NoticeService(cast(Any, None), cast(Any, None), **kw)


# ---- the sound ---------------------------------------------------------------------------


async def test_a_wav_is_read_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc_mod.shutil, "which", lambda _: None)
    assert await service().decode_audio(wav(tone(300))) == tone(300)


async def test_an_upload_over_the_cap_is_refused() -> None:
    with pytest.raises(N.NoticeRefused) as info:
        await service().decode_audio(b"\0" * (N.AUDIO_UPLOAD_MAX + 1))
    assert (info.value.reason, info.value.status) == ("audio_too_big", 413)


async def test_without_ffmpeg_only_a_wav_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc_mod.shutil, "which", lambda _: None)
    with pytest.raises(N.NoticeRefused) as info:
        await service().decode_audio(b"ID3 not really an mp3")
    assert (info.value.reason, info.value.status) == ("audio_format", 415)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_ffmpeg_decodes_a_compressed_clip_to_the_panel_rate(tmp_path: Any) -> None:
    import asyncio

    src = tmp_path / "in.wav"
    src.write_bytes(wav(tone(1500, rate=44100), rate=44100))
    out = tmp_path / "clip.ogg"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src), "-c:a", "libvorbis", str(out)
    )
    assert await proc.wait() == 0
    pcm = await service().decode_audio(out.read_bytes())
    assert abs(N.pcm_ms(pcm) - 1500) < 80


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_ffmpeg_garbage_is_unreadable() -> None:
    with pytest.raises(N.NoticeRefused) as info:
        await service().decode_audio(b"\x00\x01garbage" * 50)
    assert (info.value.reason, info.value.status) == ("audio_unreadable", 422)


async def test_a_decoder_that_cannot_start_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc_mod.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    async def boom(cmd: list[str], *, timeout_s: float) -> tuple[int, bytes, bytes]:
        raise TimeoutError

    import jbrain.media

    monkeypatch.setattr(jbrain.media, "run_media_proc", boom)
    with pytest.raises(N.NoticeRefused) as info:
        await service().decode_audio(b"not a wav")
    assert info.value.extra == {"why": "TimeoutError"}


async def test_no_tts_configured_is_503() -> None:
    with pytest.raises(N.NoticeRefused) as info:
        await service().speak("hello")
    assert (info.value.reason, info.value.status) == ("tts_unavailable", 503)


async def test_speech_too_long_to_type_is_refused() -> None:
    with pytest.raises(N.NoticeRefused) as info:
        await service(tts_url="http://tts").speak("x" * (N.SAY_CHARS + 1))
    assert info.value.extra == {"field": "say", "max_chars": N.SAY_CHARS}


async def test_speech_uses_the_owner_s_voice_and_falls_back_to_the_default() -> None:
    seen: list[dict[str, str]] = []

    async def fetch(params: dict[str, str]) -> bytes:
        seen.append(params)
        return wav(tone(800, rate=24000), rate=24000)

    class Voice:
        params = {"voice": "af_heart", "speed": "1.1"}
        lexicon: dict[str, str] = {}

    async def voice() -> Any:
        return Voice()

    pcm = await service(tts_fetch=fetch, voice=voice).speak(" Done. ")
    assert seen[-1] == {"text": "Done.", "voice": "af_heart", "speed": "1.1"}
    assert 0 < N.pcm_ms(pcm) <= 800

    async def broken() -> Any:
        raise RuntimeError("settings unreadable")

    await service(tts_fetch=fetch, voice=broken).speak("Done.")
    assert seen[-1] == {"text": "Done."}


async def test_a_tts_failure_is_503() -> None:
    async def down(params: dict[str, str]) -> bytes:
        raise httpx.ConnectError("no")

    with pytest.raises(N.NoticeRefused) as info:
        await service(tts_fetch=down).speak("hi")
    assert info.value.reason == "tts_unavailable"

    async def not_wav(params: dict[str, str]) -> bytes:
        return b"<html>"

    with pytest.raises(N.NoticeRefused):
        await service(tts_fetch=not_wav).speak("hi")


async def test_the_real_fetch_calls_the_tts_service(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tts" and request.url.params["text"] == "hi"
        return httpx.Response(200, content=wav(tone(400)))

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(svc_mod.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    pcm = await service(tts_url="http://tts/").speak("hi")
    assert N.pcm_ms(pcm) > 0


# ---- panel_notify ------------------------------------------------------------------------


class FakeNotices:
    def __init__(self, refuse: N.NoticeRefused | None = None, end: str | None = "answered") -> None:
        self.refuse = refuse
        self.end = end
        self.sent: list[dict[str, Any]] = []
        self.waited: list[float] = []

    async def send(self, **kw: Any) -> Sent:
        self.sent.append(kw)
        if self.refuse:
            raise self.refuse
        return Sent("n-1", "dev", "Jeff", "queued", 60, None, None)

    async def wait(
        self, notice_id: str, wait_s: float, device_id: str | None = None
    ) -> NoticeView | None:
        self.waited.append(wait_s)
        if self.end is None:
            return None
        return NoticeView(
            id=notice_id,
            device_id="dev",
            sender="jerv",
            title="t",
            body="",
            choices=["Yes", "No"],
            state=self.end,
            answer="Yes" if self.end == "answered" else None,
            created_at="",
            delivered_at=None,
            shown_at=None,
            finished_at=None,
            hide_after_s=None,
            image=None,
            audio_ms=None,
        )


CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id="s-1")


def tool(notices: FakeNotices, monkeypatch: pytest.MonkeyPatch | None = None):  # noqa: ANN201
    handlers = paneltools.build_panel_handlers(
        cast(NoticeService, notices),
        maker=cast(Any, None),
        blobs=cast(Any, None),
        images=cast(Any, None),
        attachments=cast(Any, None),
    )
    return handlers["panel_notify"]


async def test_a_plain_notice_is_sent_as_jerv_and_not_waited_for() -> None:
    fake = FakeNotices()
    out = await tool(fake)({"title": "Backup done", "body": "All good", "say": "Done"}, CTX)
    assert out == "Shown on the Jeff panel (notice n-1)."
    assert fake.sent[0]["sender"] == "jerv" and fake.sent[0]["say"] == "Done"
    assert fake.sent[0]["created_by"] == "jerv (session s-1)"
    assert fake.waited == []


async def test_a_question_waits_for_the_tap() -> None:
    fake = FakeNotices()
    out = await tool(fake)({"title": "Run it?", "choices": ["Yes", "No"], "wait_s": 9999}, CTX)
    assert out == "He answered on the Jeff panel: Yes."
    assert cast(ToolOutput, out).result_brief == "Yes"
    assert fake.waited == [paneltools.WAIT_MAX_S]


@pytest.mark.parametrize(
    ("end", "words"),
    [
        ("dismissed", "dismissed it"),
        ("expired", "expired"),
        (None, "expired"),
        ("shown", "No answer yet after 90 s"),
    ],
)
async def test_every_ending_is_said(end: str | None, words: str) -> None:
    out = await tool(FakeNotices(end=end))({"title": "Run it?", "choices": ["Yes", "No"]}, CTX)
    assert words in out


async def test_a_question_need_not_be_waited_for() -> None:
    fake = FakeNotices()
    out = await tool(fake)(
        {"title": "Run it?", "choices": ["Yes"], "await_answer": False, "hide_after_s": 30}, CTX
    )
    assert out.startswith("Shown on") and fake.waited == []
    assert fake.sent[0]["hide_after_s"] == 30


async def test_a_bad_wait_falls_back_to_the_default() -> None:
    fake = FakeNotices()
    await tool(fake)({"title": "t", "choices": ["a"], "wait_s": "soon"}, CTX)
    await tool(fake)({"title": "t", "choices": ["a"], "wait_s": 1}, CTX)
    assert fake.waited == [paneltools.WAIT_DEFAULT_S, paneltools.WAIT_MIN_S]


@pytest.mark.parametrize(
    ("exc", "words"),
    [
        (N.NoticeRefused("offline", 503, last_seen_s=42), "offline (last heard 42 s ago)"),
        (N.NoticeRefused("offline", 503, last_seen_s=None), "the panel is offline. Do not"),
        (N.NoticeRefused("not_accepting", 409), "switched off"),
        (N.NoticeRefused("quiet_hours", 409, until="06:30"), "quiet hours, until 06:30"),
        (N.NoticeRefused("no_notice_surface", 409), "cannot show notices yet"),
        (N.NoticeRefused("busy", 429), "4 notices are already waiting"),
        (N.NoticeRefused("which_panel", 409), "more than one panel"),
        (N.NoticeRefused("tts_unavailable", 503), "cannot speak"),
        (
            N.NoticeRefused("too_long", 422, field="title", max_bytes=60),
            "Not sent — too_long (field=title, max_bytes=60). Fix it",
        ),
        (N.NoticeRefused("audio_empty", 422), "Not sent — audio_empty. Fix it"),
    ],
)
async def test_a_refusal_is_words_not_an_error(exc: N.NoticeRefused, words: str) -> None:
    out = await tool(FakeNotices(refuse=exc))({"title": "x"}, CTX)
    assert words in out
    assert cast(ToolOutput, out).result_brief == f"refused: {exc.reason}"


async def test_a_bad_hide_is_words_not_a_crash() -> None:
    out = await tool(FakeNotices())({"title": "x", "hide_after_s": "soon"}, CTX)
    assert "whole number of seconds" in out


async def test_choices_must_be_a_list() -> None:
    assert "must be a list" in await tool(FakeNotices())({"title": "x", "choices": "Yes"}, CTX)


async def test_a_picture_is_resolved_from_the_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def resolve(image_id: str, attachment_id: str, **kw: Any) -> tuple[bytes, str] | str:
        calls.append((image_id, attachment_id))
        return (
            (b"PNGDATA", "sha")
            if image_id == "good"
            else "No generated image with that id is in this chat."
        )

    monkeypatch.setattr(paneltools, "resolve_source", resolve)
    fake = FakeNotices()
    await tool(fake)({"title": "Look", "image_id": "good"}, CTX)
    assert fake.sent[-1]["image"] == b"PNGDATA"
    missing = await tool(fake)({"title": "Look", "image_id": "bad"}, CTX)
    assert missing.startswith("No generated image")
    both = await tool(fake)({"title": "Look", "image_id": "a", "attachment_id": "b"}, CTX)
    assert both == "Give image_id or attachment_id, not both."
    assert calls == [("good", ""), ("bad", "")]
