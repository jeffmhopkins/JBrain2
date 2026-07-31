"""RapidOCR sidecar — deterministic CPU OCR (PP-OCR via ONNX Runtime).

One route: POST /ocr with the raw image bytes in the body -> {text, lines, mean_score}.
The engine LAZY-loads on the first call and UNLOADS after RAPIDOCR_IDLE_TTL_SECONDS of no
calls (the JBrain residency idiom applied to a ~15 MB engine), so an idle box pays only the
process baseline, not the loaded inference sessions. /healthz NEVER touches the engine, so a
health probe can't defeat the idle-unload. Internal-only; the api (a pinned RapidOcrClient)
is the sole caller — the sidecar holds no owner data and reasons about nothing but pixels.

docs/plans/RAPIDOCR_PLAN.md
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, Request, Response

_IDLE_TTL = float(os.environ.get("RAPIDOCR_IDLE_TTL_SECONDS", "300"))
_REAP_INTERVAL = 30.0


class _LazyEngine:
    """Loads RapidOCR on first use and frees it after an idle TTL. One lock serializes
    (re)load and inference so concurrent calls never double-instantiate or unload mid-run —
    OCR is CPU-bound, so serial on a small box is the right posture anyway."""

    def __init__(self) -> None:
        self._engine: Any = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    async def run(self, data: bytes) -> dict[str, Any]:
        async with self._lock:
            if self._engine is None:
                # Import + construct only on demand so a cold process starts light.
                from rapidocr_onnxruntime import RapidOCR

                self._engine = RapidOCR()
            self._last_used = time.monotonic()
            result = await asyncio.to_thread(self._infer, data)
            self._last_used = time.monotonic()
            return result

    def _infer(self, data: bytes) -> dict[str, Any]:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("could not decode image")
        out, _ = self._engine(img)
        lines: list[dict[str, Any]] = []
        scores: list[float] = []
        for row in out or []:
            box, text, score = row[0], str(row[1]), float(row[2])
            lines.append(
                {"text": text, "box": [[float(x), float(y)] for x, y in box], "score": score}
            )
            scores.append(score)
        text = "\n".join(line["text"] for line in lines)
        mean = sum(scores) / len(scores) if scores else 0.0
        return {"text": text, "lines": lines, "mean_score": mean}

    async def maybe_unload(self) -> None:
        async with self._lock:
            if self._engine is not None and time.monotonic() - self._last_used > _IDLE_TTL:
                self._engine = None


engine = _LazyEngine()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async def reaper() -> None:
        while True:
            await asyncio.sleep(_REAP_INTERVAL)
            await engine.maybe_unload()

    task = asyncio.create_task(reaper())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan)


@app.post("/ocr")
async def ocr(request: Request) -> Response:
    data = await request.body()
    if not data:
        return Response(
            content=json.dumps({"error": "empty body"}),
            status_code=400,
            media_type="application/json",
        )
    try:
        result = await engine.run(data)
    except ValueError as exc:
        return Response(
            content=json.dumps({"error": str(exc)}),
            status_code=400,
            media_type="application/json",
        )
    return Response(content=json.dumps(result), media_type="application/json")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    # Deliberately does NOT touch the engine — a health probe must not keep it resident and
    # defeat the idle-unload.
    return {"status": "ok"}
