"""Client for the on-box RapidOCR sidecar — deterministic CPU OCR (PP-OCR via ONNX).

Mirrors `jbrain.web.search.SearxngClient`: a pinned base URL from config (never
model-supplied) and a thin, injectable httpx transport so tests run with no network
(DEVELOPMENT.md "no network in tests"). POSTs raw image bytes to the sidecar's `/ocr` and
returns the transcription. The sidecar is image-only — a PDF is rasterized by the caller
(one image per page, via `jbrain.ingest.imageprep.pdf_page_images`), which keeps the
service a dumb, stateless OCR box. docs/plans/RAPIDOCR_PLAN.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

# A cold sidecar lazy-loads its engine on the first call (~1-2s), so the read must not time
# out under that first-request warmup.
_TIMEOUT = httpx.Timeout(60.0)


class OcrServiceError(RuntimeError):
    """OCR could not be completed — the sidecar is unconfigured, unreachable, or returned a
    non-2xx / malformed body. Surfaced to callers (the pipeline, the jerv/sandbox tools) as
    a recoverable error, never a crash."""


@dataclass(frozen=True)
class OcrResult:
    """One image's transcription: the joined text, the mean per-line confidence, and the raw
    line rows (`{text, box, score}`) for callers that want structure."""

    text: str
    mean_score: float
    lines: tuple[dict[str, object], ...] = ()


class RapidOcrClient:
    """POST an image to a pinned RapidOCR sidecar. `transport` is injectable so tests run
    against a mock with no network."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def ocr(self, data: bytes, media_type: str = "application/octet-stream") -> OcrResult:
        if not self._base_url:
            raise OcrServiceError("OCR is not configured on this instance")
        if not data:
            raise OcrServiceError("OCR needs a non-empty image")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.post(
                    f"{self._base_url}/ocr",
                    content=data,
                    headers={"Content-Type": media_type},
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            log.warning("rapidocr.failed", status=exc.response.status_code)
            raise OcrServiceError("the OCR service is unavailable right now") from exc
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("rapidocr.failed", error=repr(exc))
            raise OcrServiceError("the OCR service is unavailable right now") from exc
        if not isinstance(body, dict):
            raise OcrServiceError("the OCR service returned a malformed response")
        raw_lines = body.get("lines")
        return OcrResult(
            text=str(body.get("text") or "").strip(),
            mean_score=float(body.get("mean_score") or 0.0),
            lines=tuple(raw_lines) if isinstance(raw_lines, list) else (),
        )
