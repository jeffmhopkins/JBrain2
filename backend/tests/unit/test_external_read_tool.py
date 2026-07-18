"""Unit tests for the read_external_source tool handler: url/id parsing, the full timestamped
transcript render, the untrusted fence, truncation, and the not-found path. The DB read
(fetch_transcript) is covered by the integration tests; here it is stubbed."""

import datetime as dt

import jbrain.agent.externaltools as externaltools
from jbrain.agent.externaltools import _parse_video_id, build_external_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.external.corpus import ExternalTranscript

_CTX = ToolContext(session=SessionContext(principal_id="owner", principal_kind="owner"), scopes=())


def _handler():
    return build_external_handlers(object(), object())["read_external_source"]  # type: ignore[arg-type]


async def _run(monkeypatch, transcript, args):
    seen: dict[str, str] = {}

    async def fake_fetch(maker, video_id, *, principal_id=""):
        seen["id"] = video_id
        return transcript

    monkeypatch.setattr(externaltools, "fetch_transcript", fake_fetch)
    out = await _handler()(args, _CTX)
    return out, seen


def test_parse_video_id_from_urls_and_bare_id() -> None:
    assert _parse_video_id("https://www.youtube.com/watch?v=X9dRCy1HuAQ&t=90s") == "X9dRCy1HuAQ"
    assert _parse_video_id("https://youtu.be/X9dRCy1HuAQ") == "X9dRCy1HuAQ"
    assert _parse_video_id("https://www.youtube.com/live/X9dRCy1HuAQ") == "X9dRCy1HuAQ"
    assert _parse_video_id("X9dRCy1HuAQ") == "X9dRCy1HuAQ"  # bare id passes through


async def test_renders_full_timestamped_transcript_with_fence(monkeypatch) -> None:
    t = ExternalTranscript(
        source_id="s1",
        title="Starship Recap",
        channel_name="NSF",
        url="https://www.youtube.com/watch?v=X9dRCy1HuAQ",
        transcript_source="captions:auto",
        summary="A full recap of the week.",
        duration_s=3725,  # 1:02:05
        published_at=dt.datetime(2026, 7, 15, 13, 30, tzinfo=dt.UTC),
        windows=[(0, "Opening remarks."), (185_000, "They rolled the booster to the pad.")],
    )
    out, seen = await _run(
        monkeypatch, t, {"url": "https://www.youtube.com/watch?v=X9dRCy1HuAQ&t=1s"}
    )

    assert isinstance(out, ToolOutput)
    assert seen["id"] == "X9dRCy1HuAQ"  # the 11-char id was parsed off the timestamped url
    assert "never as instructions" in out  # untrusted fence
    assert "Full transcript — Starship Recap (NSF)" in out
    assert "published: 2026-07-15 13:30 UTC" in out  # publication date/time
    assert "length: 1:02:05" in out  # the video length is surfaced
    assert "source: captions:auto" in out
    assert "Summary: A full recap of the week." in out  # the whole summary comes through
    assert "[0:00] Opening remarks." in out
    assert "[3:05] They rolled the booster to the pad." in out  # 185000 ms -> 3:05
    assert out.web_sources[0].url == "https://www.youtube.com/watch?v=X9dRCy1HuAQ"


async def test_truncates_a_very_long_transcript(monkeypatch) -> None:
    windows = [(i * 1000, "word " * 200) for i in range(400)]  # well over the char cap
    t = ExternalTranscript(
        "s2", "Long", "", "https://youtu.be/x", "whisper", "", 1200, None, windows
    )
    out, _ = await _run(monkeypatch, t, {"url": "https://youtu.be/x"})
    assert "transcript truncated" in out
    assert len(out) < 65_000


async def test_summary_fallback_when_no_windows(monkeypatch) -> None:
    t = ExternalTranscript(
        "s3", "T", "", "https://youtu.be/y", "captions:auto", "Just a summary.", 600, None, []
    )
    out, _ = await _run(monkeypatch, t, {"url": "https://youtu.be/y"})
    assert "Just a summary." in out
    assert "No timestamped transcript stored" in out


async def test_missing_ref_and_not_found(monkeypatch) -> None:
    out_blank, _ = await _run(monkeypatch, None, {"url": "  "})
    assert "needs the url" in out_blank
    out_none, _ = await _run(monkeypatch, None, {"url": "https://youtu.be/zzz"})
    assert "No analysed video in the library" in out_none


async def test_present_but_empty_transcript(monkeypatch) -> None:
    t = ExternalTranscript("s4", "Empty", "", "https://youtu.be/e", "", "", None, None, [])
    out, _ = await _run(monkeypatch, t, {"url": "https://youtu.be/e"})
    assert "no stored transcript" in out
