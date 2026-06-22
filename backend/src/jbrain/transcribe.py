"""Audio transcription client (whisper.cpp via the on-box llama-swap gateway).

Transcription is audio in, text out — not an LLM completion — so, like the TEI
embedding client (jbrain.embed) and the gateway admin client
(jbrain.llm.local_gateway), it lives OUTSIDE the LLM adapter: CLAUDE.md rule 1
governs completions, and transcription is neither billed nor routed by task. The
model is served by the same llama-swap gateway the `local-llm` profile builds;
its load/unload lifecycle is the job's and tool's concern, not this client's.

The fakeable-protocol seam the embed client uses: an injected httpx transport
lets tests answer deterministically and never touch the network. A dead/booting
gateway makes the caller fail normally; queue backoff covers the cold-load window.
"""

import json
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class Transcript:
    """One transcription result: the full text plus the detected language, if the
    backend reported one (whisper detects it; None when the response omits it)."""

    text: str
    language: str | None = None


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
                data={"model": self._model, "response_format": "json"},
            )
            resp.raise_for_status()
            raw = resp.text
        # Tolerant parse across whisper.cpp builds: the OpenAI shape is {"text": ...}
        # (optionally with "language"), but a build that ignores response_format may
        # answer with the plain transcript — accept that (and a bare JSON string) too.
        try:
            body: object = json.loads(raw)
        except ValueError:
            return Transcript(text=raw.strip())
        if isinstance(body, str):
            return Transcript(text=body.strip())
        text = body.get("text", "") if isinstance(body, dict) else ""
        language = body.get("language") if isinstance(body, dict) else None
        return Transcript(
            text=str(text).strip(),
            language=str(language) if isinstance(language, str) and language else None,
        )
