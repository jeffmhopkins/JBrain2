"""A router shim that drives the OWNER'S BOX through the debug console's async-job
endpoint, so the committed eval scorers (jbrain.evals.*) run unchanged against the
real local model.

`DebugRouter.complete(...)` has the same shape the scorers call (and the same shape
LlmRouter exposes), but instead of a provider SDK it submits the prompt as a debug
completion job and polls — async so a minutes-long local extraction never holds an
HTTP request open past the Cloudflare tunnel's ~100s edge timeout. ONE job at a
time: the box is a single GPU, and concurrent jobs contend and stall.

The capability token is read from JBRAIN_DEBUG_TOKEN (the minted base64 payload),
NEVER stored in the repo. Owner-run only; nothing here is imported by the app or CI.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class _Result:
    parsed: Any
    text: str
    usage: _Usage


def decode_token() -> tuple[str, str]:
    """(host, key) from the minted payload in JBRAIN_DEBUG_TOKEN — never the repo."""
    payload = os.environ.get("JBRAIN_DEBUG_TOKEN", "").strip()
    if not payload:
        raise SystemExit("set JBRAIN_DEBUG_TOKEN (the minted payload) to run against the box")
    d = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return d["u"].rstrip("/"), d["k"]


class DebugRouter:
    """Drop-in for the scorers' `router`: routes `.complete` to the box via the
    debug async-job API. One job at a time (single-GPU serial)."""

    def __init__(self, *, max_wait: float = 600.0, poll: float = 5.0) -> None:
        self._base, key = decode_token()
        self._headers = {"Authorization": f"Bearer {key}"}
        self._max_wait = max_wait
        self._poll = poll
        self._client = httpx.AsyncClient(timeout=120)

    async def complete(
        self,
        task: str,
        *,
        system: str,
        user_text: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 16384,
        strength: str | None = None,
    ) -> _Result:
        req = {
            "system": system,
            "user_text": user_text,
            "task": task,  # the box routes by its live per-task model override
            "json_schema": json_schema,
            "max_tokens": max_tokens,
        }
        submit = await self._client.post(
            f"{self._base}/api/debug/complete-async", headers=self._headers, json=req
        )
        # Surface the real cause (401 expired token, 429 rate limit, 5xx box down)
        # instead of an opaque KeyError on a non-job error body.
        if submit.status_code >= 400:
            raise RuntimeError(f"box submit failed: HTTP {submit.status_code} {submit.text[:200]}")
        job_id = submit.json()["job_id"]
        t0 = time.time()
        while time.time() - t0 < self._max_wait:
            await asyncio.sleep(self._poll)
            try:
                resp = await self._client.get(
                    f"{self._base}/api/debug/jobs/{job_id}", headers=self._headers
                )
                st = resp.json()
            except httpx.HTTPError:  # a flaky poll is fine — keep waiting
                continue
            if st["status"] == "done":
                result = st["result"]
                parsed = result.get("parsed")
                if parsed is None and result.get("text"):
                    try:
                        parsed = json.loads(result["text"])
                    except json.JSONDecodeError:
                        parsed = None
                # CompleteOut flattens token counts to the top level (no nested
                # `usage` object), so read them directly.
                return _Result(
                    parsed=parsed,
                    text=result.get("text", ""),
                    usage=_Usage(
                        result.get("input_tokens", 0), result.get("output_tokens", 0)
                    ),
                )
            if st["status"] == "error":
                raise RuntimeError(f"box job error: {st.get('error')}")
        raise TimeoutError(f"box job {job_id} did not finish within {self._max_wait}s")

    async def aclose(self) -> None:
        await self._client.aclose()
