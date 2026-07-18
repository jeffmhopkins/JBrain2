"""Unit tests for the read_external_video tool handler: url/id parsing, the full timestamped
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
    return build_external_handlers(object(), object())["read_external_video"]  # type: ignore[arg-type]


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


# --- show_external_video: the video-analysis card ------------------------------------


def _show_handler():
    return build_external_handlers(object(), object())["show_external_video"]  # type: ignore[arg-type]


async def _run_show(monkeypatch, transcript, args):
    async def fake_fetch(maker, video_id, *, principal_id=""):
        return transcript

    monkeypatch.setattr(externaltools, "fetch_transcript", fake_fetch)
    return await _show_handler()(args, _CTX)


async def test_show_emits_the_video_analysis_card(monkeypatch) -> None:
    t = ExternalTranscript(
        source_id="s1",
        title="Starship Recap",
        channel_name="NSF",
        url="https://www.youtube.com/watch?v=X9dRCy1HuAQ",
        transcript_source="captions:auto",
        summary="A recap.",
        duration_s=1386,
        published_at=None,
        windows=[(0, "Opening."), (185_000, "Booster to the pad.")],
        video_id="X9dRCy1HuAQ",
        provider="youtube",
        duration_ms=1_386_000,
        frames=[{"t_ms": 43_312, "caption": "A rocket on the pad.", "thumb_id": "sha"}],
    )
    out = await _run_show(monkeypatch, t, {"url": "https://youtu.be/X9dRCy1HuAQ?t=90"})

    assert isinstance(out, ToolOutput)
    assert out.view is not None and out.view.view == "video_analysis"
    data = out.view.data
    assert data["source"] == "stream"
    assert data["youtube_id"] == "X9dRCy1HuAQ"  # embeddable YouTube id
    assert data["summary"] == "A recap." and data["duration_ms"] == 1_386_000
    assert data["transcript_source"] == "captions:auto"
    # Frames carry t_ms + caption only (no inline thumbnail → the card renders markers).
    assert data["frames"] == [{"t_ms": 43_312, "caption": "A rocket on the pad."}]
    assert data["transcript"] == {"text": "Opening.\nBooster to the pad."}
    assert 'Showing "Starship Recap" — NSF.' in out  # the brief spoken line


async def test_show_uses_stored_cued_transcript_for_the_synced_tab(monkeypatch) -> None:
    # When the word/cue-level transcript was stored (0135), the card gets it verbatim (words +
    # text) so the transcript tab syncs; otherwise (the other show tests) it falls back to plain
    # window text.
    cued = {
        "text": "Booster to the pad.",
        "words": [
            {"text": "Booster", "start_ms": 6000, "end_ms": 6400},
            {"text": "to", "start_ms": 6400, "end_ms": 6500},
        ],
    }
    t = ExternalTranscript(
        "s7",
        "T",
        "",
        "https://youtu.be/x",
        "whisper",
        "s",
        100,
        None,
        [(0, "window text")],
        video_id="x",
        provider="youtube",
        duration_ms=100_000,
        frames=[],
        cued_transcript=cued,
    )
    out = await _run_show(monkeypatch, t, {"url": "https://youtu.be/x"})
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript"] == cued  # the stored words drive the synced tab
    assert out.view.data["transcript"]["words"][0]["text"] == "Booster"


async def test_show_falls_back_to_window_text_without_cues(monkeypatch) -> None:
    t = ExternalTranscript(
        "s8",
        "T",
        "",
        "https://youtu.be/x",
        "captions:only",
        "s",
        100,
        None,
        [(0, "line one"), (5000, "line two")],
        video_id="x",
        provider="youtube",
        duration_ms=100_000,
        frames=[],
    )
    out = await _run_show(monkeypatch, t, {"url": "https://youtu.be/x"})
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["transcript"] == {"text": "line one\nline two"}  # plain fallback


async def test_show_non_youtube_has_no_embed_id(monkeypatch) -> None:
    t = ExternalTranscript(
        "s2",
        "Clip",
        "",
        "https://vimeo.com/1",
        "whisper",
        "s",
        10,
        None,
        [(0, "hi")],
        video_id="1",
        provider="vimeo",
        duration_ms=10_000,
        frames=[],
    )
    out = await _run_show(monkeypatch, t, {"url": "https://vimeo.com/1"})
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["youtube_id"] == ""  # only YouTube embeds


async def test_show_not_found(monkeypatch) -> None:
    out = await _run_show(monkeypatch, None, {"url": "https://youtu.be/zzz"})
    assert "No analysed video in the library" in out


class _FakeBlobs:
    def __init__(self, jpeg: bytes) -> None:
        self.jpeg = jpeg

    async def put(self, data: bytes) -> str:
        return "sha"

    async def get(self, sha256: str) -> bytes:
        return self.jpeg


async def test_show_inlines_thumbnails_when_the_blob_store_has_them(monkeypatch) -> None:
    # With a blob store, a frame's thumb_id is redeemed into an inline thumbnail; without one
    # (the other show tests) the frame stays a bare marker.
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (320, 180), (10, 120, 200)).save(buf, format="JPEG")
    blobs = _FakeBlobs(buf.getvalue())

    t = ExternalTranscript(
        "s5",
        "T",
        "",
        "https://youtu.be/x",
        "captions:auto",
        "s",
        100,
        None,
        [(0, "hi")],
        video_id="x",
        provider="youtube",
        duration_ms=100_000,
        frames=[{"t_ms": 1000, "caption": "c", "thumb_id": "sha1"}],
    )

    async def fake_fetch(maker, video_id, *, principal_id=""):
        return t

    monkeypatch.setattr(externaltools, "fetch_transcript", fake_fetch)
    built = build_external_handlers(object(), object(), blobs=blobs)  # type: ignore[arg-type]
    handler = built["show_external_video"]
    out = await handler({"url": "https://youtu.be/x"}, _CTX)

    assert isinstance(out, ToolOutput) and out.view is not None
    frame = out.view.data["frames"][0]
    assert frame["t_ms"] == 1000 and frame["caption"] == "c"
    assert frame["thumb_data_uri"].startswith("data:image/jpeg;base64,")  # blob inlined


async def test_show_frame_degrades_to_marker_when_blob_missing(monkeypatch) -> None:
    class _EmptyBlobs:
        async def put(self, data: bytes) -> str:
            return "sha"

        async def get(self, sha256: str) -> bytes:
            raise KeyError(sha256)  # purged/missing

    t = ExternalTranscript(
        "s6",
        "T",
        "",
        "https://youtu.be/x",
        "captions:auto",
        "s",
        100,
        None,
        [(0, "hi")],
        video_id="x",
        provider="youtube",
        duration_ms=100_000,
        frames=[{"t_ms": 1000, "caption": "c", "thumb_id": "gone"}],
    )

    async def fake_fetch(maker, video_id, *, principal_id=""):
        return t

    monkeypatch.setattr(externaltools, "fetch_transcript", fake_fetch)
    built = build_external_handlers(object(), object(), blobs=_EmptyBlobs())  # type: ignore[arg-type]
    out = await built["show_external_video"]({"url": "https://youtu.be/x"}, _CTX)
    assert isinstance(out, ToolOutput) and out.view is not None
    assert out.view.data["frames"][0] == {"t_ms": 1000, "caption": "c"}  # marker, no thumb


# --- remove_external_video: stages an owner-approved removal proposal ------------------


class _FakeProposals:
    def __init__(self) -> None:
        self.staged = None

    async def stage(self, ctx, *, principal_id, spec):  # noqa: ANN001
        self.staged = spec
        return "prop-1"


async def test_remove_stages_a_removal_proposal(monkeypatch) -> None:
    t = ExternalTranscript(
        "s9",
        "Starship Recap",
        "",
        "https://youtu.be/x",
        "whisper",
        "s",
        100,
        None,
        [],
        video_id="x",
        provider="youtube",
    )

    async def fake_fetch(maker, video_id, *, principal_id=""):
        return t

    monkeypatch.setattr(externaltools, "fetch_transcript", fake_fetch)
    props = _FakeProposals()
    handler = build_external_handlers(object(), object(), proposals=props)[  # type: ignore[arg-type]
        "remove_external_video"
    ]
    out = await handler({"url": "https://youtu.be/x"}, _CTX)

    assert isinstance(out, ToolOutput)
    # jerv PROPOSES — it returns a proposal chip, deletes nothing itself.
    assert out.proposal is not None and out.proposal.kind == "remove-library-video"
    assert "won't delete anything until you approve" in out
    spec = props.staged
    assert spec is not None and spec.kind == "remove-library-video" and spec.domain == "external"
    node = spec.nodes[0]
    assert node.op == "delete_external_video" and node.preview["source_id"] == "s9"
    assert "Starship Recap" in node.label


async def test_remove_not_found(monkeypatch) -> None:
    async def fake_fetch(maker, video_id, *, principal_id=""):
        return None

    monkeypatch.setattr(externaltools, "fetch_transcript", fake_fetch)
    handler = build_external_handlers(object(), object(), proposals=_FakeProposals())[  # type: ignore[arg-type]
        "remove_external_video"
    ]
    out = await handler({"url": "https://youtu.be/zzz"}, _CTX)
    assert "No analysed video in the library" in out


async def test_remove_unavailable_without_a_proposal_repo() -> None:
    handler = build_external_handlers(object(), object())["remove_external_video"]  # type: ignore[arg-type]
    out = await handler({"url": "https://youtu.be/x"}, _CTX)
    assert "isn't available" in out
