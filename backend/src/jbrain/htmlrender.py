"""Client for the on-box `htmlrender` sidecar — HTML + CSS in, PNG out.

Mirrors `jbrain.vision.rapidocr.RapidOcrClient`: a pinned base URL from config (never
model-supplied) and an injectable httpx transport so tests run with no network
(DEVELOPMENT.md "no network in tests").

**This is a general service, not a canvas detail.** The canvas `html` op is its first
caller; a later flowchart, comparison table, or report-card tool should render through
here too rather than inventing its own rasterizer or — the thing
`docs/reference/DESIGN.md` forbids — shipping model-authored markup to the PWA, whose
view registry is closed and whose invariants bar model-authored markup, URLs and
colour. Rendering to pixels is what makes rich model-authored HTML *safe*: the owner's
browser receives an image, and an image cannot execute.

**Two properties callers must not undo.** The sidecar is egress-free by compose
topology (its network is declared `internal: true`) and aborts every non-`data:`
request itself, because the markup it renders is untrusted. And it must never be sent
owner images: render a TRANSPARENT overlay and composite it over the photograph on
this side, inside the domain firewall.
"""

from __future__ import annotations

import base64

import httpx
import structlog

log = structlog.get_logger()

# A cold sidecar launches Chromium on the first call (~1-3s) after an idle unload, so
# the read must not time out under that warmup.
_TIMEOUT = httpx.Timeout(45.0)

# Mirrors the sidecar's own caps so an oversized request fails here, cheaply, with a
# message a tool can hand back to the model rather than as a 413 from a container.
MAX_SIDE = 4096
MAX_PIXELS = 8_000_000
MAX_HTML_BYTES = 512_000


class HtmlRenderError(RuntimeError):
    """The page could not be rendered — the sidecar is unconfigured, unreachable, or the
    markup failed. Recoverable: surfaced to the model as a tool error, never a crash."""


class HtmlRenderClient:
    """POST a fragment of HTML to the pinned renderer and get PNG bytes back."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def render(
        self,
        html: str,
        *,
        width: int,
        height: int,
        transparent: bool = True,
        wait_ms: int = 0,
        scale: float = 1.0,
    ) -> bytes:
        """Render `html` at `width`x`height` and return PNG bytes.

        Transparent by default because the primary caller composites the result over a
        photograph it must not send to a browser."""
        if not self._base_url:
            raise HtmlRenderError("the HTML renderer is not configured on this instance")
        if not html.strip():
            raise HtmlRenderError("nothing to render — the html was empty")
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise HtmlRenderError(
                f"that html is too large to render ({MAX_HTML_BYTES // 1000}KB limit)"
            )
        if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
            raise HtmlRenderError(f"render size must be within 1..{MAX_SIDE} on each side")
        if width * height > MAX_PIXELS:
            raise HtmlRenderError("that render is too many pixels")
        payload = {
            "html": html,
            "width": width,
            "height": height,
            "transparent": transparent,
            "wait_ms": wait_ms,
            "scale": scale,
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.post(f"{self._base_url}/render", json=payload)
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as exc:
            detail = _detail(exc.response)
            log.warning("htmlrender.rejected", status=exc.response.status_code, detail=detail)
            raise HtmlRenderError(f"the renderer rejected that page: {detail}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("htmlrender.unreachable", error=str(exc))
            raise HtmlRenderError(f"the HTML renderer could not be reached: {exc}") from exc
        encoded = body.get("png_base64") if isinstance(body, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise HtmlRenderError("the renderer returned no image")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise HtmlRenderError("the renderer returned a corrupt image") from exc


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or f"HTTP {response.status_code}"
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"][:200]
    return f"HTTP {response.status_code}"
