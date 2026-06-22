"""Audio transcription client (whisper.cpp via the on-box llama-swap gateway).

Transcription is audio in, text out — not an LLM completion — so, like the TEI
embedding client (jbrain.embed) and the gateway admin client
(jbrain.llm.local_gateway), it lives OUTSIDE the LLM adapter: CLAUDE.md rule 1
governs completions, and transcription is neither billed nor routed by task. The
model is served by the same llama-swap gateway the `local-llm` profile builds;
its load/unload lifecycle is the job's and tool's concern, not this client's.

We request `verbose_json` so the response carries per-segment confidence
(`avg_logprob`) and, when the build emits them, per-token timestamps and
probabilities — which we fold into WORDS for the karaoke transcript UI
(docs/mocks/audio-transcript-approved.html). The parse is deliberately tolerant:
whisper.cpp's verbose_json varies across builds, so we accept token start/end in
seconds OR centiseconds, take per-token probability when present and fall back to
the segment's avg_logprob otherwise, and degrade to text-only (no words) if the
server answers with plain text. Confidence is always a probability in [0, 1].

The fakeable-protocol seam the embed client uses: an injected httpx transport
lets tests answer deterministically and never touch the network.
"""

import json
import math
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class Word:
    """One spoken word with its place in the audio and how sure the model is.

    `start_ms`/`end_ms` are offsets into the clip (for playback sync); `confidence`
    is a probability in [0, 1] — per-token when the build reports it, else the
    word's segment average. Drives the transcript's gradient color + word-seek."""

    text: str
    start_ms: int
    end_ms: int
    confidence: float


@dataclass(frozen=True)
class Transcript:
    """One transcription result: the full text, the detected language (None when
    omitted), the per-word breakdown (empty when the build emits no segments), and
    the clip duration in ms (None when unknown)."""

    text: str
    language: str | None = None
    words: tuple[Word, ...] = field(default_factory=tuple)
    duration_ms: int | None = None


class TranscribeClient(Protocol):
    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        """Transcribe one audio clip to text. `filename`/`media_type` ride the
        multipart upload so the backend can sniff the container format."""
        ...


class WhisperCppClient:
    """whisper.cpp HTTP client via the gateway's OpenAI-compatible endpoint
    (POST /v1/audio/transcriptions, multipart). The gateway routes by the `model`
    form field to the matching upstream, loading it on first request."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url
        self._model = model
        self._timeout = timeout
        self._transport = transport

    async def transcribe(self, audio: bytes, *, filename: str, media_type: str) -> Transcript:
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout, transport=self._transport
        ) as client:
            # Relative path: the gateway base URL already ends in /v1 (the repo's
            # OpenAI-base convention, like local_llm_url), so the endpoint is just
            # audio/transcriptions — not /v1/... which httpx would append, doubling
            # the segment.
            resp = await client.post(
                "audio/transcriptions",
                files={"file": (filename, audio, media_type)},
                data={"model": self._model, "response_format": "verbose_json"},
            )
            resp.raise_for_status()
            raw = resp.text
        return parse_transcript(raw)


def parse_transcript(raw: str) -> Transcript:
    """Parse a whisper-server response into a Transcript. Tolerant of plain-text
    bodies, bare JSON strings, and verbose_json across whisper.cpp builds."""
    try:
        body: object = json.loads(raw)
    except ValueError:
        return Transcript(text=raw.strip())
    if isinstance(body, str):
        return Transcript(text=body.strip())
    if not isinstance(body, dict):
        return Transcript(text="")

    language = body.get("language")
    language = str(language) if isinstance(language, str) and language else None
    segments = body.get("segments")
    words = _words_from_segments(segments) if isinstance(segments, list) else ()
    text = str(body.get("text") or "").strip()
    if not text and words:  # some builds omit the joined text — rebuild from words.
        text = " ".join(w.text for w in words).strip()

    duration_ms = _duration_ms(body.get("duration"), words)
    return Transcript(text=text, language=language, words=words, duration_ms=duration_ms)


def _words_from_segments(segments: list[Any]) -> tuple[Word, ...]:
    """Fold whisper segments → words. Each segment carries tokens (sub-word units);
    a token that starts a new word begins with whitespace (BPE convention), so we
    accumulate continuation tokens (punctuation, word-internal pieces) onto the
    current word. A word's time spans its tokens; its confidence is the mean of its
    tokens' probabilities, falling back to the segment's avg_logprob."""
    out: list[Word] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_conf = _logprob_to_conf(seg.get("avg_logprob"))
        tokens = seg.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            # No token detail — emit the whole segment as one "word" so sync + a
            # (segment-level) confidence still work.
            out.extend(_segment_as_word(seg, seg_conf))
            continue
        cur: dict[str, Any] | None = None
        for tok in tokens:
            if not isinstance(tok, dict):
                continue
            piece = str(tok.get("text") or tok.get("word") or "")
            if not piece or _is_special(piece):
                continue
            prob = _token_conf(tok, seg_conf)
            start, end = _token_span(tok)
            if piece[:1].isspace() or cur is None:
                if cur is not None:
                    out.append(_finish(cur))
                cur = {"text": piece.strip(), "start": start, "end": end, "probs": [prob]}
            else:
                cur["text"] += piece
                cur["end"] = end if end is not None else cur["end"]
                cur["probs"].append(prob)
            if cur["start"] is None and start is not None:
                cur["start"] = start
        if cur is not None:
            out.append(_finish(cur))
    return tuple(w for w in out if w.text)


def _segment_as_word(seg: dict[str, Any], seg_conf: float) -> list[Word]:
    text = str(seg.get("text") or "").strip()
    if not text:
        return []
    start, end = _to_ms(seg.get("start")), _to_ms(seg.get("end"))
    return [Word(text=text, start_ms=start or 0, end_ms=end or (start or 0), confidence=seg_conf)]


def _finish(cur: dict[str, Any]) -> Word:
    start = cur["start"] if cur["start"] is not None else 0
    end = cur["end"] if cur["end"] is not None else start
    probs = cur["probs"] or [0.0]
    return Word(
        text=cur["text"], start_ms=start, end_ms=max(end, start), confidence=sum(probs) / len(probs)
    )


def _token_span(tok: dict[str, Any]) -> tuple[int | None, int | None]:
    # OpenAI shape uses start/end in seconds; whisper.cpp also emits t0/t1 in
    # centiseconds. _to_ms normalizes either to milliseconds.
    if "start" in tok or "end" in tok:
        return _to_ms(tok.get("start")), _to_ms(tok.get("end"))
    return _to_ms(tok.get("t0"), centiseconds=True), _to_ms(tok.get("t1"), centiseconds=True)


def _token_conf(tok: dict[str, Any], seg_conf: float) -> float:
    for key in ("p", "probability", "prob"):
        v = tok.get(key)
        if isinstance(v, int | float):
            return _clamp(float(v))
    return seg_conf


def _logprob_to_conf(avg_logprob: object) -> float:
    """A segment's avg_logprob → a [0, 1] confidence. Whisper logprobs are roughly
    -1..0 for clean speech; exp() maps that to a usable (uncalibrated) probability.
    Missing → a neutral 0.6 (machine-heard, not author-written — the Guards floor)."""
    if isinstance(avg_logprob, int | float):
        return _clamp(math.exp(float(avg_logprob)))
    return 0.6


def _duration_ms(duration: object, words: tuple[Word, ...]) -> int | None:
    if isinstance(duration, int | float) and duration > 0:
        return int(float(duration) * 1000)
    return max((w.end_ms for w in words), default=None)


def _to_ms(value: object, *, centiseconds: bool = False) -> int | None:
    if not isinstance(value, int | float):
        return None
    return int(float(value) * (10 if centiseconds else 1000))


def _is_special(piece: str) -> bool:
    """Whisper special/timestamp tokens (e.g. "[_BEG_]", "<|0.00|>") aren't words."""
    s = piece.strip()
    return s.startswith("[_") or (s.startswith("<|") and s.endswith("|>"))


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x
