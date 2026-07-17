"""Dependency smoke test for analyze_stream (docs/archive/STREAM_ANALYSIS_PLAN.md,
Wave 1; CLAUDE.md rule #8 single-source-of-truth). Fails fast if `yt-dlp` (stream
URL → direct media URL resolution) is missing from a synced environment — so a
broken `uv sync` / dev-setup step reddens here instead of deep in the tool wiring.
ffmpeg is a system package with its own gate (`jbrain.media.ffmpeg_available`)."""

from __future__ import annotations

from jbrain.stream import ytdlp_available


def test_ytdlp_importable_and_gated() -> None:
    # The gate the registry uses to offer/drop the sidecar; in a synced env it is True.
    assert ytdlp_available() is True

    import yt_dlp

    # The resolution surface the tool uses: build an extractor and call extract_info.
    assert hasattr(yt_dlp, "YoutubeDL")
