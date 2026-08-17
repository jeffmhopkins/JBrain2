"""RapidOCR sidecar — deterministic CPU OCR (PP-OCR via ONNX Runtime).

Two routes: POST /ocr with the raw image bytes -> {text, lines, mean_score}, and POST
/detect/faces -> {faces: [{box, score}]} via OpenCV's bundled YuNet.
The engine LAZY-loads on the first call and UNLOADS after RAPIDOCR_IDLE_TTL_SECONDS of no
calls (the JBrain residency idiom applied to a ~15 MB engine), so an idle box pays only the
process baseline, not the loaded inference sessions. /healthz NEVER touches the engine, so a
health probe can't defeat the idle-unload. Internal-only; the api (a pinned RapidOcrClient)
is the sole caller — the sidecar holds no owner data and reasons about nothing but pixels.

**Why faces live here.** OpenCV is already pinned in this image for the OCR stack, and it
ships YuNet — a ~340 KB ONNX face detector — so the capability costs no new dependency and
no new container. It is worth having because the alternative is a vision model guessing,
and VLM grounding UNDERCOUNTS faces silently: six boxes returned for fourteen people looks
exactly like a complete answer (docs/plans/AGENT_CANVAS_PLAN.md §6.5).

docs/plans/RAPIDOCR_PLAN.md · docs/plans/AGENT_CANVAS_PLAN.md W5
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


class _LazyFaceDetector:
    """YuNet, loaded on first use and freed after the same idle TTL as the OCR engine.

    The model file ships inside the opencv wheel, so there is nothing to download and no
    weights volume — `cv2.data.haarcascades` is NOT used (that is the old cascade); YuNet
    is a small ONNX net loaded through `cv2.FaceDetectorYN`."""

    def __init__(self) -> None:
        self._net: Any = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    async def run(self, data: bytes, min_score: float) -> dict[str, Any]:
        async with self._lock:
            image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("not a decodable image")
            height, width = image.shape[:2]
            if self._net is None:
                self._net = cv2.FaceDetectorYN.create(
                    _yunet_path(), "", (width, height), min_score, 0.3, 5000
                )
            else:
                self._net.setInputSize((width, height))
                self._net.setScoreThreshold(min_score)
            self._last_used = time.monotonic()
            _count, raw = self._net.detect(image)
            faces = []
            for row in raw if raw is not None else []:
                x, y, w, h = (float(v) for v in row[:4])
                # Clamp: YuNet can return a box slightly outside the frame on an edge
                # face, and a negative origin would crop to nothing downstream.
                x1, y1 = max(0.0, x), max(0.0, y)
                x2, y2 = min(float(width), x + w), min(float(height), y + h)
                if x2 <= x1 or y2 <= y1:
                    continue
                faces.append(
                    {"box": [int(x1), int(y1), int(x2), int(y2)], "score": round(float(row[14]), 4)}
                )
            return {"faces": faces, "width": width, "height": height}

    async def reap(self, ttl: float) -> None:
        async with self._lock:
            if self._net is not None and time.monotonic() - self._last_used > ttl:
                self._net = None


_YUNET_PATH = os.environ.get("YUNET_MODEL_PATH", "/app/models/face_detection_yunet.onnx")


def _yunet_path() -> str:
    """The YuNet ONNX, fetched at image build time.

    It is NOT bundled in the opencv wheel — only the `cv2.FaceDetectorYN` API is — so a
    missing file means the image was built without it. Raise a clear ValueError (which
    the route turns into a 400) rather than letting OpenCV fail obscurely, so the caller
    can fall back to model grounding and SAY it fell back."""
    import pathlib

    if pathlib.Path(_YUNET_PATH).is_file():
        return _YUNET_PATH
    raise ValueError(
        f"face detection is unavailable on this box: no YuNet model at {_YUNET_PATH}"
    )


engine = _LazyEngine()
faces_engine = _LazyFaceDetector()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    async def reaper() -> None:
        while True:
            await asyncio.sleep(_REAP_INTERVAL)
            await engine.maybe_unload()
            # The face net idles out on the same schedule; an image the owner never
            # asked about again must not hold a loaded net resident.
            await faces_engine.reap(_IDLE_TTL)

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


@app.post("/detect/faces")
async def detect_faces(request: Request) -> Response:
    """Deterministic face boxes for the crop lane. Returns [] honestly when there are
    none — an empty list is a real answer, unlike a model's silent undercount."""
    data = await request.body()
    if not data:
        return Response(
            content=json.dumps({"error": "empty body"}),
            status_code=400,
            media_type="application/json",
        )
    try:
        min_score = float(request.query_params.get("min_score", "0.6"))
    except ValueError:
        min_score = 0.6
    try:
        result = await faces_engine.run(data, min_score)
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
