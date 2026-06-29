"""Management client for the llama-swap local gateway — runtime model state.

This is runtime-state management, not a functional LLM call, so it lives outside
the LLM adapter: it speaks llama-swap's admin HTTP API to report and control which
models are resident in memory:
  - GET  /running                      → models currently loaded
  - POST /api/models/unload/{model}    → unload one model
  - GET  /upstream/{model}/health      → proxy a request, which makes the gateway
                                         load the model (llama-swap has no explicit
                                         load endpoint; loading is request-driven)
  - POST /upstream/{model}/v1/chat/completions (1 token, discarded) → warm the
                                         inference path after load so the first real
                                         turn isn't the slow one (see `load`)
  - GET  /logs                         → recent gateway + upstream stdout, which the
                                         loading bar mines for the llama.cpp model-load
                                         percentage (a real "weights read in" signal,
                                         since we run --no-mmap)

Best-effort by design. The settings screen must render even when the gateway is
down, still cold, or too old to expose these endpoints, so `running()` swallows
every error and returns an empty set; only an explicit `unload()` surfaces a
failure (the operator asked for an action, so they get told if it didn't work).

The OpenAI base URL ends in `/v1`; the admin endpoints sit at the root, so we
strip that suffix once here.
"""

from __future__ import annotations

import re
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger()


class LocalGatewayError(Exception):
    """A load/unload call the gateway rejected or couldn't be reached for."""


class LocalGateway(Protocol):
    """The runtime-state surface consumers depend on (report/unload/load), so a
    caller takes the capability rather than the concrete HTTP client — the in-memory
    test fake satisfies it structurally, the same seam as the `ImageGen` protocol."""

    async def running(self) -> set[str]: ...

    async def unload(self, served_model: str) -> None: ...

    async def load(self, served_model: str) -> None: ...

    # NOTE: load_progress() is deliberately NOT on this protocol. It's an optional,
    # best-effort extension only the jcode status probes (via getattr), so keeping it off
    # the protocol lets the many structural test fakes satisfy LocalGateway without each
    # having to stub it. Add it here only if a typed caller must depend on it.


class LocalGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 3.0,
    ):
        self._root = base_url.rstrip("/").removesuffix("/v1")
        self._transport = transport
        self._timeout = timeout

    async def running(self) -> set[str]:
        """Served-model names currently loaded, or an empty set on ANY failure
        (unreachable, non-2xx, malformed, or an old build without /running)."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/running")
                resp.raise_for_status()
                return _parse_running(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.info("local_gateway.running_unavailable", error=str(exc))
            return set()

    async def unload(self, served_model: str) -> None:
        """Unload one model from memory. Raises LocalGatewayError on any failure."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(f"{self._root}/api/models/unload/{served_model}")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LocalGatewayError(str(exc)) from exc

    async def load(self, served_model: str) -> None:
        """Load `served_model` into memory AND warm it for inference. The health probe
        makes llama-swap load the model (request-driven; with --no-mmap the weights are
        read into RAM before it returns). But "weights resident" isn't "inference-ready":
        the first forward pass still pays the inference path — KV-cache allocation for the
        full context, CUDA graph capture, kernel warm-up — which otherwise lands on the
        user's first real turn (it feels like the model reloads: slow first token, fast
        after). So after the probe we force a single-token generation whose output is
        discarded — a readiness probe, the inference-path analog of the health GET, not a
        functional LLM call. Raises LocalGatewayError if the model can't load; the warm-up
        itself is best-effort (the model is resident regardless — a failed warm-up just
        leaves that cost on first use, the prior behaviour). Generous timeout: a cold 80B
        reads tens of GB of weights."""
        load_timeout = max(self._timeout, 120.0)
        try:
            async with httpx.AsyncClient(timeout=load_timeout, transport=self._transport) as client:
                resp = await client.get(f"{self._root}/upstream/{served_model}/health")
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LocalGatewayError(str(exc)) from exc
        await self._warm(served_model)

    async def _warm(self, served_model: str) -> None:
        """Exercise the inference path with one discarded token. Best-effort: the model is
        already loaded, so a warm-up failure is logged, not raised — it only means the
        first real turn pays the warm-up cost (no worse than before this warm-up existed)."""
        body = {
            "model": served_model,
            "messages": [{"role": "user", "content": "warmup"}],
            "max_tokens": 1,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(
                timeout=max(self._timeout, 120.0), transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._root}/upstream/{served_model}/v1/chat/completions", json=body
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.info("local_gateway.warm_skipped", model=served_model, error=str(exc))

    async def tail_logs(self) -> str:
        """The gateway's own recent stdout — the llama-swap wrapper plus the upstream
        llama-server, interleaved exactly as the engine emits them. This is the inference
        engine's account of a turn (the slot acquired when a request starts, the slot
        RELEASED when its generation ends), so a debug session can see whether a client
        disconnect — a Stop — actually halts decoding or the engine keeps running to its
        own limit. Distinct from the container-log proxy: same source as `load_progress`
        but returned raw. Raises LocalGatewayError on any failure — the operator asked for
        this, so a miss is surfaced, not swallowed (unlike best-effort load_progress)."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/logs")
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPError as exc:
            raise LocalGatewayError(str(exc)) from exc

    async def load_progress(self) -> float | None:
        """A real load fraction (0..1) for the model currently coming onto the box, parsed
        best-effort from the gateway's recent logs — or None when it can't be determined
        (gateway down, no /logs endpoint, or the build emits no parseable progress). The
        loading bar follows this when present and falls back to a time estimate otherwise,
        so None is a soft miss, never an error. Only one model loads at a time (we evict
        the others first), so the latest progress line in the log is unambiguous."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(f"{self._root}/logs")
                resp.raise_for_status()
                return _parse_load_progress(resp.text)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("local_gateway.logs_unavailable", error=str(exc))
            return None


# llama.cpp surfaces model-load progress on its stderr (captured by llama-swap's /logs).
# The exact wording shifts across builds, so match tolerantly: a recent log line that pairs
# a load/tensor/weight keyword with a percentage. We take the LAST such line — progress
# only climbs, and the freshest line is the truest read of how far the load has gotten.
_LOAD_KEYWORD_RE = re.compile(r"(?i)load|tensor|weight")
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def _parse_load_progress(text: str) -> float | None:
    last: float | None = None
    for line in text.splitlines():
        if not _LOAD_KEYWORD_RE.search(line):
            continue
        m = _PERCENT_RE.search(line)
        if m is None:
            continue
        pct = float(m.group(1))
        if 0.0 <= pct <= 100.0:
            last = pct / 100.0
    return last


def _parse_running(payload: object) -> set[str]:
    """Tolerant parse of /running across llama-swap versions: accept a bare list,
    or an object wrapping the list under a common key; pull a model name from each
    item whether it's a string or an object."""
    items: object = payload
    if isinstance(payload, dict):
        items = next(
            (payload[k] for k in ("running", "models", "data") if isinstance(payload.get(k), list)),
            [],
        )
    out: set[str] = set()
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                out.add(item)
            elif isinstance(item, dict):
                name = next(
                    (item[k] for k in ("model", "id", "name") if isinstance(item.get(k), str)),
                    None,
                )
                if name:
                    out.add(name)
    return out
