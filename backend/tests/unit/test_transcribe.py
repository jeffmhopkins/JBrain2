"""WhisperCppClient: the verbose_json request shape and the tolerant parse into
words (text + ms span + confidence). A MockTransport answers offline.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from jbrain.transcribe import Transcript, WhisperCppClient, Word, parse_transcript

WAV = b"RIFF....fake-audio-bytes"

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> WhisperCppClient:
    return WhisperCppClient(
        "http://gw/v1", "whisper-large-v3", transport=httpx.MockTransport(handler)
    )


VERBOSE = {
    "language": "en",
    "duration": 1.1,
    "text": "Hello world.",
    "segments": [
        {
            "avg_logprob": -0.2,
            "text": " Hello world.",
            "start": 0.0,
            "end": 1.1,
            "tokens": [
                {"text": " Hello", "start": 0.0, "end": 0.5, "p": 0.9},
                {"text": " world", "start": 0.5, "end": 1.0, "p": 0.4},
                {"text": ".", "start": 1.0, "end": 1.1, "p": 0.8},
            ],
        }
    ],
}


@pytest.mark.asyncio
async def test_posts_verbose_json_multipart_and_parses_words() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = req.content
        return httpx.Response(200, json=VERBOSE)

    result = await make_client(handler).transcribe(WAV, filename="memo.wav", media_type="audio/wav")

    assert seen["url"] == "http://gw/v1/audio/transcriptions"
    assert b'name="model"' in bytes(seen["body"])  # type: ignore[arg-type]
    assert b"verbose_json" in bytes(seen["body"])  # type: ignore[arg-type]
    assert result.text == "Hello world." and result.language == "en"
    assert result.duration_ms == 1100
    # Sub-word tokens fold into words; punctuation attaches to its word; confidence
    # is the mean of a word's token probs; spans are milliseconds.
    assert result.words == (
        Word(text="Hello", start_ms=0, end_ms=500, confidence=0.9),
        Word(text="world.", start_ms=500, end_ms=1100, confidence=pytest.approx(0.6)),
    )


def test_token_centiseconds_t0_t1_are_normalized() -> None:
    body = {
        "segments": [
            {
                "avg_logprob": -0.1,
                "tokens": [
                    {"text": " hi", "t0": 0, "t1": 30, "p": 0.95},
                    {"text": " there", "t0": 30, "t1": 80, "p": 0.7},
                ],
            }
        ]
    }
    tr = parse_transcript(json.dumps(body))
    assert tr.text == "hi there"
    assert tr.words[0] == Word("hi", 0, 300, 0.95)  # 30 cs -> 300 ms
    assert tr.words[1] == Word("there", 300, 800, 0.7)


def test_segment_without_tokens_uses_avg_logprob_as_one_word() -> None:
    body = {"segments": [{"text": " whole line", "start": 0.0, "end": 2.0, "avg_logprob": -0.7}]}
    tr = parse_transcript(json.dumps(body))
    assert len(tr.words) == 1
    w = tr.words[0]
    assert w.text == "whole line" and w.start_ms == 0 and w.end_ms == 2000
    assert w.confidence == pytest.approx(0.4966, abs=1e-3)  # exp(-0.7)


def test_missing_token_prob_falls_back_to_segment_confidence() -> None:
    body = {
        "segments": [{"avg_logprob": -0.2, "tokens": [{"text": " hi", "start": 0, "end": 0.4}]}]
    }
    tr = parse_transcript(json.dumps(body))
    assert tr.words[0].confidence == pytest.approx(0.8187, abs=1e-3)  # exp(-0.2)


def test_special_and_timestamp_tokens_are_dropped() -> None:
    body = {
        "segments": [
            {
                "avg_logprob": -0.1,
                "tokens": [
                    {"text": "[_BEG_]", "start": 0, "end": 0},
                    {"text": "<|0.00|>", "start": 0, "end": 0},
                    {"text": " word", "start": 0.0, "end": 0.5, "p": 0.9},
                ],
            }
        ]
    }
    tr = parse_transcript(json.dumps(body))
    assert [w.text for w in tr.words] == ["word"]


def test_plain_text_body_degrades_to_text_only() -> None:
    assert parse_transcript("  just text  ") == Transcript(text="just text")


def test_bare_json_string_is_accepted() -> None:
    assert parse_transcript('"hello"') == Transcript(text="hello")


@pytest.mark.asyncio
async def test_http_error_propagates() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model loading")

    with pytest.raises(httpx.HTTPStatusError):
        await make_client(handler).transcribe(WAV, filename="a.wav", media_type="audio/wav")
