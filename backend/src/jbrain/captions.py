"""Provider captions as a transcript source for `analyze_stream` (a faster alternative to
whisper). Many YouTube videos already carry captions — human/manual (`subtitles`) or the
provider's ASR (`automatic_captions`) — and yt-dlp surfaces both in the info dict it
already produces when we resolve the stream, at no extra resolve cost. When a track is
available we can fetch + parse it into the SAME `Transcript`/`Word` shape whisper produces
and skip the GPU pass entirely: it's instant, covers the WHOLE video (no ~30-min in-turn
cap), and — for the word-level `json3` format — sidesteps whisper's silence-drift because
the timings are per-word from the source.

The caption URL is a second signed outbound leg, so it carries the same egress discipline
as the media URL: the host passes the shared SSRF guard before we GET it, redirects are
refused (a 30x to a private target can't slip past), the download is size-capped, and it's
best-effort — any failure returns None so the caller falls back to whisper.

Selection + parsing are pure (yt-dlp / httpx never touched) so they unit-test directly;
the fetch takes an injectable httpx transport like the other outbound clients.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from jbrain.transcribe import Transcript, Word
from jbrain.web.fetch import WebFetchError, guard_public_host

log = structlog.get_logger()

# Captions carry no per-word confidence, so give them a fixed high value — the transcript
# UI's gradient stays neutral rather than implying a model uncertainty we don't have.
_CAPTION_CONF = 0.9
# Prefer word-level, compact formats first; json3 alone carries per-word offsets.
_EXT_RANK = {"json3": 0, "srv3": 1, "srv2": 2, "srv1": 3, "vtt": 4, "ttml": 5}
_MAX_CAPTION_BYTES = 8_000_000  # a whole-video caption file is small; cap a pathological one
_CAPTION_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class CaptionTrack:
    """A selected caption track to fetch: its URL, container `ext`, whether it's a human
    (`manual`) or ASR (`auto`) track, and its language."""

    url: str
    ext: str
    kind: str  # manual | auto
    lang: str


def select_caption(info: Any, *, prefer_langs: tuple[str, ...] = ("en",)) -> CaptionTrack | None:
    """Pick the best caption track from a yt-dlp info dict, or None if it has none. Human
    `subtitles` win over `automatic_captions`; within a kind a preferred language wins; and
    within a language the most word-level format (`json3`) wins."""
    for kind, key in (("manual", "subtitles"), ("auto", "automatic_captions")):
        tracks = info.get(key) if isinstance(info, dict) else None
        picked = _pick_lang_track(tracks, prefer_langs)
        if picked is not None:
            lang, fmt = picked
            return CaptionTrack(
                url=str(fmt["url"]), ext=str(fmt.get("ext") or ""), kind=kind, lang=lang
            )
    return None


def _pick_lang_track(tracks: Any, prefer_langs: tuple[str, ...]) -> tuple[str, dict] | None:
    if not isinstance(tracks, dict) or not tracks:
        return None

    def lang_rank(lang: str) -> int:
        base = lang.split("-")[0].lower().removeprefix("a.")  # 'a.en' = an auto marker
        for i, pref in enumerate(prefer_langs):
            if base == pref or lang.lower() == pref:
                return i
        return len(prefer_langs)

    for lang in sorted(tracks.keys(), key=lang_rank):
        fmts = tracks.get(lang)
        if not isinstance(fmts, list):
            continue
        usable = [f for f in fmts if isinstance(f, dict) and f.get("url")]
        if usable:
            best = min(usable, key=lambda f: _EXT_RANK.get(str(f.get("ext") or ""), 99))
            return lang, best
    return None


async def fetch_caption_transcript(
    track: CaptionTrack,
    headers: dict[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Transcript | None:
    """Fetch + parse a caption track into a Transcript, SSRF-guarded and best-effort (None
    on a refused host, a redirect, an HTTP error, or an empty parse — the caller then
    falls back to whisper). `headers` are yt-dlp's request headers for the source."""
    try:
        guard_public_host(track.url, skip_dns=transport is not None)
    except WebFetchError as exc:
        log.info("captions.guard_refused", error=str(exc))
        return None
    try:
        async with httpx.AsyncClient(
            timeout=_CAPTION_TIMEOUT_S, transport=transport, follow_redirects=False
        ) as client:
            resp = await client.get(track.url, headers=dict(headers or {}))
            if resp.is_redirect:  # a redirect could dodge the host guard — refuse it
                return None
            resp.raise_for_status()
            data = resp.content[:_MAX_CAPTION_BYTES]
    except (httpx.HTTPError, WebFetchError) as exc:
        log.info("captions.fetch_failed", error=repr(exc))
        return None
    transcript = parse_captions(data, track.ext)
    return transcript if (transcript.words or transcript.text) else None


def parse_captions(data: bytes, ext: str) -> Transcript:
    """Parse caption bytes → Transcript. `json3` carries per-word timing; other formats
    (vtt/srv*) are cue-level, so each cue becomes one coarse 'word'."""
    if ext.lower() == "json3" or data[:1] == b"{":
        return _parse_json3(data)
    return _parse_vtt(data.decode("utf-8", "replace"))


def _parse_json3(data: bytes) -> Transcript:
    """YouTube json3: `events[]` cues, each with `tStartMs`/`dDurationMs` and `segs[]` word
    pieces carrying a `tOffsetMs` from the cue start — the per-word timing we want."""
    try:
        body = json.loads(data)
    except ValueError:
        return Transcript(text="")
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        return Transcript(text="")
    words: list[Word] = []
    for ev in events:
        if not isinstance(ev, dict) or not isinstance(ev.get("segs"), list):
            continue
        t0 = _to_int(ev.get("tStartMs"))
        dur = _to_int(ev.get("dDurationMs"))
        ev_end = t0 + dur if dur else None
        starts = [
            (t0 + _to_int(seg.get("tOffsetMs")), str(seg.get("utf8") or "").strip())
            for seg in ev["segs"]
            if isinstance(seg, dict) and str(seg.get("utf8") or "").strip()
        ]
        for i, (start, txt) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else (ev_end if ev_end else start)
            words.append(
                Word(text=txt, start_ms=start, end_ms=max(end, start), confidence=_CAPTION_CONF)
            )
    return _transcript_from_words(words)


_VTT_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")
_VTT_TAG = re.compile(r"<[^>]+>")


def _parse_vtt(text: str) -> Transcript:
    """WebVTT / SRT: `start --> end` cues, each becoming one coarse cue-level 'word' (the
    whole cue text). Inline tags (`<c>`, `<00:00:01.000>`) are stripped."""
    words: list[Word] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if "-->" in lines[i]:
            ts = _VTT_TS.findall(lines[i])
            if len(ts) >= 2:
                start, end = _vtt_ms(ts[0]), _vtt_ms(ts[1])
                i += 1
                buf: list[str] = []
                while i < len(lines) and lines[i].strip():
                    buf.append(lines[i])
                    i += 1
                cue = _VTT_TAG.sub("", " ".join(buf)).strip()
                if cue:
                    words.append(
                        Word(
                            text=cue,
                            start_ms=start,
                            end_ms=max(end, start),
                            confidence=_CAPTION_CONF,
                        )
                    )
        i += 1
    return _transcript_from_words(words)


def _transcript_from_words(words: list[Word]) -> Transcript:
    text = " ".join(w.text for w in words).strip()
    return Transcript(
        text=text, words=tuple(words), duration_ms=(words[-1].end_ms if words else None)
    )


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _vtt_ms(parts: tuple[str, str, str, str]) -> int:
    h, m, s, ms = (int(p) for p in parts)
    return ((h * 60 + m) * 60 + s) * 1000 + ms
