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


@dataclass(frozen=True)
class FaceBox:
    """One detected face in ORIGINAL-image pixels, corners not extents."""

    x1: int
    y1: int
    x2: int
    y2: int
    score: float


class FaceDetectUnavailable(OcrServiceError):
    """The sidecar has no face model (or none is configured). Distinct from a general
    failure because the caller's right response is to fall back to model grounding AND
    say it fell back — never to silently return fewer faces."""


async def detect_faces(
    client: RapidOcrClient, data: bytes, *, min_score: float = 0.6
) -> tuple[FaceBox, ...]:
    """Deterministic face boxes via the sidecar's YuNet (AGENT_CANVAS_PLAN W5).

    Worth the extra hop because the alternative — asking a vision model to box every
    face — fails in one direction and fails quietly: it misses people in a crowd and
    reports no error. An empty tuple here is a real "no faces", not a shrug."""
    # Kept a module function rather than a method so the OCR client stays a single-purpose
    # object; the sidecar just happens to host both, because opencv is already in it.
    base = client._base_url  # noqa: SLF001 - same module, one pinned base URL
    if not base:
        raise FaceDetectUnavailable("face detection is not configured on this instance")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=client._transport) as http:  # noqa: SLF001
            resp = await http.post(
                f"{base}/detect/faces",
                content=data,
                params={"min_score": min_score},
                headers={"Content-Type": "application/octet-stream"},
            )
            if resp.status_code == 400:
                raise FaceDetectUnavailable(_detail(resp))
            resp.raise_for_status()
            body = resp.json()
    except FaceDetectUnavailable:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("rapidocr.faces_failed", error=repr(exc))
        raise FaceDetectUnavailable("the face detector is unavailable right now") from exc
    rows = body.get("faces") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise FaceDetectUnavailable("the face detector returned a malformed response")
    out: list[FaceBox] = []
    for row in rows:
        box = row.get("box") if isinstance(row, dict) else None
        if not (isinstance(box, list) and len(box) == 4):
            continue
        try:
            x1, y1, x2, y2 = (int(v) for v in box)
        except (TypeError, ValueError):
            continue
        if x2 > x1 and y2 > y1:
            out.append(FaceBox(x1, y1, x2, y2, float(row.get("score") or 0.0)))
    return tuple(out)


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "face detection failed"
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"]
    return "face detection failed"
