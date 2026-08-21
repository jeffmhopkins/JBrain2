"""Client for the on-box `htmlrender` sidecar — HTML + CSS in, PNG out.

Mirrors `jbrain.vision.rapidocr.RapidOcrClient`: a pinned base URL from config (never
model-supplied) and an injectable httpx transport so tests run with no network
(DEVELOPMENT.md "no network in tests").

**This is a general service, not a canvas detail.** The canvas `html` op and the
standalone `render_html` tool are its callers; a later flowchart or report-card tool
should render through here too rather than inventing its own rasterizer or — the thing
`docs/reference/DESIGN.md` forbids — shipping model-authored markup to the PWA, whose
view registry is closed and whose invariants bar model-authored markup, URLs and
colour. Rendering to pixels is what makes rich model-authored HTML *safe*: the owner's
browser receives an image, and an image cannot execute.

**Two properties callers must not undo.** The sidecar is egress-free by compose
topology (its network is declared `internal: true`) and aborts every non-`data:`
request itself, because the markup it renders is untrusted. And it must never be sent
owner images: render a TRANSPARENT overlay and composite it over the photograph on
this side, inside the domain firewall.

**Sizing has two modes.** Pass `height` when you are compositing into a rect you
already sized (the canvas). Omit it for a standalone card and the page is measured and
shot at its own content height — which is what keeps a short card from being padded out
to a guessed height, the thing that reads as an empty second frame inside the app's own
image card.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()

# A cold sidecar launches Chromium on the first call (~1-3s) after an idle unload, so
# the read must not time out under that warmup.
_TIMEOUT = httpx.Timeout(45.0)

# Mirrors the sidecar's own caps so an oversized request fails here, cheaply, with a
# message a tool can hand back to the model rather than as a 413 from a container.
# Both size caps are on the OUTPUT image (after `scale`), which is what actually has to
# be encoded and shipped — not on the CSS viewport.
MAX_SIDE = 4096
MAX_PIXELS = 8_000_000
MAX_HTML_BYTES = 512_000

# The page grounds the sidecar knows, by name. A name rather than a colour for three
# reasons: nothing a caller sends is interpolated into the sidecar's stylesheet; each
# ground carries a default ink with real contrast against it (these renders are read by a
# vision model as well as by the owner); and both are the PWA's own `--surface` tokens, so
# the image sits flush in the card that frames it instead of showing a second edge.
THEMES = frozenset({"light", "dark"})


class HtmlRenderError(RuntimeError):
    """The page could not be rendered — the sidecar is unconfigured, unreachable, or the
    markup failed. Recoverable: surfaced to the model as a tool error, never a crash."""


@dataclass(frozen=True)
class Rendered:
    """A rendered page: the PNG plus what the sidecar actually produced.

    `width`/`height` are the image's own dimensions (read from its header on the far
    side), not the request's — a caller that sizes a frame from them would crop the
    picture if they disagreed. `clipped` is true when the content ran past the size cap
    and was cut off, so a caller can say so instead of showing half a table as if it
    were the whole one."""

    png: bytes
    width: int
    height: int
    clipped: bool = False


def max_css_height(width: int, scale: float = 1.0) -> int:
    """The tallest CSS height whose output still fits both caps at this width/scale."""
    out_width = max(1, round(width * scale))
    return max(1, int(min(MAX_SIDE, MAX_PIXELS // max(1, out_width)) / scale))


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
        height: int | None = None,
        transparent: bool = True,
        theme: str = "dark",
        pad: int = 0,
        wait_ms: int = 0,
        scale: float = 1.0,
    ) -> Rendered:
        """Render `html` at `width` and return the PNG with its real dimensions.

        `height` pins the page to exactly that many CSS pixels; omitting it measures the
        content and shoots that. Transparent by default because the canvas caller
        composites the result over a photograph it must not send to a browser."""
        if not self._base_url:
            raise HtmlRenderError("the HTML renderer is not configured on this instance")
        if not html.strip():
            raise HtmlRenderError("nothing to render — the html was empty")
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise HtmlRenderError(
                f"that html is too large to render ({MAX_HTML_BYTES // 1000}KB limit)"
            )
        if not 0 < width <= MAX_SIDE or round(width * scale) > MAX_SIDE:
            raise HtmlRenderError(f"render width must be within 1..{MAX_SIDE} after scaling")
        if height is not None and not 0 < height <= max_css_height(width, scale):
            raise HtmlRenderError("that render is too many pixels")
        if theme not in THEMES:
            raise HtmlRenderError(f"theme must be one of: {', '.join(sorted(THEMES))}")
        payload = {
            "html": html,
            "width": width,
            "height": height,
            "transparent": transparent,
            "theme": theme,
            "pad": pad,
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
            png = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise HtmlRenderError("the renderer returned a corrupt image") from exc
        return Rendered(
            png=png,
            width=_dim(body.get("width"), width),
            height=_dim(body.get("height"), height or 0),
            clipped=bool(body.get("clipped")),
        )


def _dim(value: object, fallback: int) -> int:
    """A dimension the sidecar reported, or the request's own as a floor. An older
    sidecar that predates the honest-dimensions response must not yield a zero here —
    a card sized from 0 collapses its aspect ratio."""
    return value if isinstance(value, int) and value > 0 else fallback


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or f"HTTP {response.status_code}"
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"][:200]
    return f"HTTP {response.status_code}"
