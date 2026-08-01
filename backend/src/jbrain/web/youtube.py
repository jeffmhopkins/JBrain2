"""A lightweight YouTube view for `web_fetch` (docs/reference/ASSISTANT.md "Agent selection").

Scraping a YouTube watch page as HTML yields a JS shell, not content — so a bare `web_fetch`
of a video URL was useless. `analyze_video` reads a video properly, but it is expensive (media
download + whisper/GPU). Most of what the model wants from a video — what it is, who made it,
what it's about, and what was said — is available far more cheaply: yt-dlp already surfaces the
title, channel, and description in its metadata, and many videos carry provider captions
(human `subtitles` or ASR `automatic_captions`) that `jbrain.captions` fetches and parses
without ever touching the media. This composes those into one Markdown view that the fetch
tool then pages/keyword-jumps like any other page — so a long transcript is navigable with the
same `offset`/`find` the rest of `web_fetch` uses.

No media download, no GPU, no whisper: the cost is one yt-dlp metadata resolve plus (when a
track exists) one size-capped, SSRF-guarded caption GET — the same egress discipline the media
path carries. `analyze_video` remains the tool for a video with no captions, or when the frames
(not just the words) matter. The resolver and caption fetcher are injected so this unit-tests
with fakes, no yt-dlp or network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from jbrain.captions import CaptionTrack
from jbrain.stream import ResolvedStream, StreamError
from jbrain.transcribe import Transcript

log = structlog.get_logger()

# Blocking yt-dlp resolve, run off the event loop by the caller (asyncio.to_thread in prod, a
# direct call in tests). Takes the URL, returns the metadata+caption bundle or raises StreamError.
Resolver = Callable[[str], ResolvedStream]
# Async caption fetch+parse (jbrain.captions.fetch_caption_transcript), best-effort → None.
CaptionFetcher = Callable[[CaptionTrack, dict[str, str]], Awaitable[Transcript | None]]
# Runs a blocking callable off the event loop (asyncio.to_thread), injected so tests stay sync.
# Typed loosely (`...`/Any) to accept to_thread's generic signature; the use site re-narrows.
RunBlocking = Callable[..., Awaitable[Any]]


def _hms(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _render(resolved: ResolvedStream, transcript: str) -> str:
    """Compose the video's metadata + transcript into one Markdown body (the tool adds the
    title/url header). The description is the uploader's own summary; the transcript is the
    provider captions when present. A caption-less video says so and points at analyze_video."""
    head: list[str] = []
    if resolved.channel_name:
        head.append(f"**Channel:** {resolved.channel_name}")
    if resolved.duration_s:
        head.append(f"**Duration:** {_hms(resolved.duration_s)}")
    parts = ["\n".join(head)] if head else []
    description = resolved.description.strip()
    if description:
        parts.append("## Description\n\n" + description)
    transcript = transcript.strip()
    if transcript:
        parts.append("## Transcript (captions)\n\n" + transcript)
    else:
        parts.append(
            "_No captions were available for this video. Use analyze_video if you need the"
            " spoken content or the visuals._"
        )
    return "\n\n".join(parts)


async def youtube_page(
    url: str,
    *,
    resolver: Resolver,
    caption_fetcher: CaptionFetcher,
    run_blocking: RunBlocking,
) -> tuple[str, str] | None:
    """(title, Markdown body) for a YouTube URL — title, channel, description, and caption
    transcript — or None when the video can't be resolved (private, geo-blocked, not really a
    video), so the tool falls back to a normal HTML fetch. Best-effort throughout: a caption
    failure just drops the transcript, never the whole view."""
    try:
        resolved: ResolvedStream = await run_blocking(resolver, url)
    except StreamError as exc:
        log.info("youtube.resolve_failed", url=url, error=str(exc))
        return None
    transcript = ""
    if resolved.caption is not None:
        try:
            parsed = await caption_fetcher(resolved.caption, resolved.http_headers)
        except Exception as exc:  # noqa: BLE001 - a caption miss must not sink the whole view
            log.info("youtube.caption_failed", url=url, error=repr(exc))
            parsed = None
        if parsed is not None:
            transcript = parsed.text
    return resolved.title, _render(resolved, transcript)
