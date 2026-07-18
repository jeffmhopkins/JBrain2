"""Provider captions as a transcript source (jbrain.captions): selecting the best track
from a yt-dlp info dict, parsing json3 (per-word) and vtt (cue-level) into our Word shape,
and the SSRF-guarded, best-effort fetch. yt-dlp and the network are never touched — the
selection + parse are pure and the fetch takes an injected httpx transport."""

import json

import httpx
import pytest

from jbrain.captions import (
    CaptionTrack,
    fetch_caption_transcript,
    parse_captions,
    select_caption,
)

# --- selection ------------------------------------------------------------------------


def _fmts(*exts: str) -> list[dict]:
    return [{"ext": e, "url": f"https://cc.example.com/{e}"} for e in exts]


def test_select_prefers_manual_over_automatic() -> None:
    info = {
        "subtitles": {"en": _fmts("vtt")},
        "automatic_captions": {"en": _fmts("json3")},
    }
    track = select_caption(info)
    assert track is not None
    assert track.kind == "manual" and track.ext == "vtt" and track.lang == "en"


def test_select_prefers_json3_within_a_language() -> None:
    # json3 carries per-word timing, so it wins over cue-level formats in the same track.
    info = {"automatic_captions": {"en": _fmts("vtt", "srv1", "json3")}}
    track = select_caption(info)
    assert track is not None and track.ext == "json3" and track.kind == "auto"


def test_select_prefers_a_requested_language() -> None:
    info = {"subtitles": {"es": _fmts("json3"), "en": _fmts("json3")}}
    track = select_caption(info, prefer_langs=("en",))
    assert track is not None and track.lang == "en"


def test_select_returns_none_without_captions() -> None:
    assert select_caption({}) is None
    assert select_caption({"subtitles": {}, "automatic_captions": {}}) is None
    assert select_caption(None) is None


def test_select_skips_a_track_with_no_usable_url() -> None:
    info = {"subtitles": {"en": [{"ext": "json3"}]}, "automatic_captions": {"en": _fmts("vtt")}}
    track = select_caption(info)  # the manual track has no url → fall through to the auto one
    assert track is not None and track.kind == "auto" and track.ext == "vtt"


# --- json3 parsing --------------------------------------------------------------------


def test_parse_json3_yields_per_word_timings() -> None:
    body = {
        "events": [
            {
                "tStartMs": 1000,
                "dDurationMs": 900,
                "segs": [
                    {"utf8": "Hello", "tOffsetMs": 0},
                    {"utf8": " world", "tOffsetMs": 400},
                ],
            }
        ]
    }
    tr = parse_captions(json.dumps(body).encode(), "json3")
    assert [(w.text, w.start_ms, w.end_ms) for w in tr.words] == [
        ("Hello", 1000, 1400),  # ends at the next seg's onset
        ("world", 1400, 1900),  # last seg ends at the cue end (tStart + dDuration)
    ]
    assert tr.text == "Hello world"


def test_parse_json3_ignores_blank_and_malformed_segs() -> None:
    body = {
        "events": [
            {"tStartMs": 0, "segs": [{"utf8": "\n"}, {"utf8": "hi", "tOffsetMs": 0}]},
            {"tStartMs": 5000},  # no segs
            "junk",
        ]
    }
    tr = parse_captions(json.dumps(body).encode(), "json3")
    assert [w.text for w in tr.words] == ["hi"]


def test_parse_json3_detected_by_content_when_ext_unknown() -> None:
    body = {"events": [{"tStartMs": 0, "segs": [{"utf8": "x", "tOffsetMs": 0}]}]}
    tr = parse_captions(json.dumps(body).encode(), "")  # leading '{' → json3 path
    assert tr.text == "x"


def test_parse_bad_json_degrades_to_empty() -> None:
    assert parse_captions(b"{not json", "json3").text == ""


# --- vtt parsing ----------------------------------------------------------------------


def test_parse_vtt_makes_one_word_per_cue_and_strips_tags() -> None:
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.500\n"
        "<c>Hello</c> <00:00:02.000>there\n\n"
        "00:00:04.000 --> 00:00:05.000\n"
        "friend\n"
    )
    tr = parse_captions(vtt.encode(), "vtt")
    assert [(w.text, w.start_ms, w.end_ms) for w in tr.words] == [
        ("Hello there", 1000, 3500),
        ("friend", 4000, 5000),
    ]


def test_parse_vtt_handles_comma_millis() -> None:
    srt = "1\n00:00:00,500 --> 00:00:01,500\nword\n"
    tr = parse_captions(srt.encode(), "vtt")
    assert tr.words[0].start_ms == 500 and tr.words[0].end_ms == 1500


# --- fetch (SSRF-guarded, best-effort) -------------------------------------------------


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_parses_a_json3_track() -> None:
    body = {"events": [{"tStartMs": 0, "segs": [{"utf8": "hi", "tOffsetMs": 0}]}]}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(body).encode())

    track = CaptionTrack(url="https://cc.example.com/x.json3", ext="json3", kind="auto", lang="en")
    tr = await fetch_caption_transcript(track, {}, transport=_transport(handler))
    assert tr is not None and tr.text == "hi"


@pytest.mark.asyncio
async def test_fetch_refuses_a_redirect() -> None:
    # A 30x could dodge the host guard, so a redirect is refused (→ None, whisper fallback).
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/x"})

    track = CaptionTrack(url="https://cc.example.com/x.json3", ext="json3", kind="auto", lang="en")
    assert await fetch_caption_transcript(track, {}, transport=_transport(handler)) is None


@pytest.mark.asyncio
async def test_fetch_refuses_a_non_http_url() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return httpx.Response(200, content=b"{}")

    track = CaptionTrack(url="file:///etc/passwd", ext="json3", kind="auto", lang="en")
    assert await fetch_caption_transcript(track, {}, transport=_transport(handler)) is None


@pytest.mark.asyncio
async def test_fetch_returns_none_on_http_error() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    track = CaptionTrack(url="https://cc.example.com/x.json3", ext="json3", kind="auto", lang="en")
    assert await fetch_caption_transcript(track, {}, transport=_transport(handler)) is None


@pytest.mark.asyncio
async def test_fetch_returns_none_on_empty_parse() -> None:
    # A track that parses to nothing (no words, no text) is None so the caller falls back.
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"events": []}).encode())

    track = CaptionTrack(url="https://cc.example.com/x.json3", ext="json3", kind="auto", lang="en")
    assert await fetch_caption_transcript(track, {}, transport=_transport(handler)) is None
