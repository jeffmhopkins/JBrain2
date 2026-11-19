"""The box, as the creature page sees it from the simulator: the site's files from this
checkout, and the `/api` answers `growbot/server/serve.py` gives, kept in memory.

Nothing here reaches the real box except the model, and only in live mode (`llm.py`): a run
reads the persona once and never writes it back, so a simulated Dave can't change the real
one's soul, memories or settings.
"""

from __future__ import annotations

import json
import mimetypes
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

SITE = Path(__file__).resolve().parents[2] / "site"
ORIGIN = "https://growbot.sim"
ROBOT = "gb-sim001"
PERSONA = "00000000-0000-4000-8000-00000000da7e"
# body.json's stock calibration, as the PWA saves it for a robot.
STOCK_CAL: dict[str, Any] = {
    "L_SIGN": -1,
    "R_SIGN": 1,
    "L_OFF": 0,
    "R_OFF": 0,
    "IMU_SIGN": [1, -1, 1],
    "gain": 1,
    "turn": 0,
}
TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".wasm": "application/wasm",
    ".json": "application/json",
    ".onnx": "application/octet-stream",
    ".tflite": "application/octet-stream",
    ".task": "application/octet-stream",
}

# The one place the simulator serves the page other than as written. A phone runs the face
# detector on its GPU in ~30 ms; headless Chromium has only a software GL, where the same
# model takes ~250 ms a frame, past the tracker's 400 ms staleness line once a frame waits.
# MediaPipe's CPU path runs the same model, so the boxes are the phone's, at the phone's pace.
SIM_PATCHES: dict[str, tuple[bytes, bytes]] = {
    "/v3/agent-face-track.mjs": (b"delegate:'GPU'", b"delegate:'CPU'"),
}

# A chat request in, the model's reply text out.
Model = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    ctype: str = "application/json"
    # The model call this answers, for the run to log once the page has it.
    call: dict[str, Any] | None = None

    @classmethod
    def json(cls, obj: Any, status: int = 200) -> Reply:
        return cls(status, json.dumps(obj).encode())


def silent_wav(ms: int = 200, rate: int = 16000) -> bytes:
    """A short silent WAV: what the voice says, as far as a headless run needs to hear."""
    n = rate * ms // 1000
    data = b"\x00\x00" * n
    head = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    head += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    return head + b"data" + struct.pack("<I", len(data)) + data


def policy_id(slug: str) -> str:
    """A library id for a kept walk, stable across runs (the box's are UUIDs too)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"growbot-sim/{slug}"))


# What a creature keeps of itself through a wipe: who it is, whose it is, its body and the
# reflexes its person taught it. The rest (its log, facts, wants, the day's scratch and the
# identity grown from them) is history. The PWA's Forget history is the same wipe
# (backend jbrain.growbot.souls.forgotten), which the backend suite holds them to.
KEEP_GOALS = ("owner", "selfName", "inputLanguage", "replyLanguage", "call")


def forget(soul: dict[str, Any]) -> dict[str, Any]:
    """A soul with its memories wiped: what a "fresh" Dave run starts from."""
    goals = soul.get("goals") or {}
    scratch = soul.get("scratch") or {}
    keep = {"reflexes": scratch["reflexes"]} if isinstance(scratch.get("reflexes"), list) else {}
    out = {**soul, "log": [], "facts": [], "scratch": keep, "identity": ""}
    out.pop("map", None)  # his house map is learned too (growbot-map.js)
    out["goals"] = {k: goals[k] for k in KEEP_GOALS if k in goals}
    # A creature named in conversation keeps that name only in its history, so the wipe
    # would take it: the persona's own name stands in, as if the owner had confirmed it.
    name = str((soul.get("meta") or {}).get("name") or "")[:32]
    if not out["goals"].get("selfName"):
        out["goals"]["selfName"] = name
    # The page reseeds a short identity with a newborn's stock one (and its stock name), and
    # a box soul's selfName doesn't survive the load, so the name goes in a full sentence.
    me = out["goals"].get("selfName") or name
    out["identity"] = (
        f"I am {me}. I walk on two legs, and I want to explore the real world with my person."
        if me
        else ""
    )
    return out


@dataclass
class FakeBox:
    model: Model
    soul: dict[str, Any] | None = None
    prefs: dict[str, str] = field(default_factory=dict)
    cal: dict[str, Any] = field(default_factory=lambda: dict(STOCK_CAL))
    # Library id -> the policy JSON's path; skills maps look/forward/back to ids.
    policies: dict[str, Path] = field(default_factory=dict)
    skills: dict[str, str] | None = None
    site: Path = SITE
    version: int = 1
    # Every model call and answer, for the run's timeline.
    calls: list[dict[str, Any]] = field(default_factory=list)
    t0: float = field(default_factory=time.monotonic)
    # The run's clock for call times, so they line up with its timeline.
    clock: Callable[[], float] | None = None

    def _t(self) -> float:
        return self.clock() if self.clock else round(time.monotonic() - self.t0, 2)

    # ------------------------------------------------------------------ dispatch

    async def handle(self, method: str, url: str, body: bytes | None) -> Reply:
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        if not path.startswith("/api/"):
            return self.static(path)
        return await self.api(method, path[len("/api/") :], query, body or b"")

    def static(self, path: str) -> Reply:
        got = self._file(path)
        patch = SIM_PATCHES.get(path)
        if patch is not None and got.status == 200:
            old, new = patch
            if got.body.count(old) != 1:
                raise RuntimeError(f"sim patch for {path} no longer matches the page")
            got.body = got.body.replace(old, new)
        return got

    def _file(self, path: str) -> Reply:
        root = self.site.resolve()
        full = (root / path.lstrip("/")).resolve()
        if not full.is_relative_to(root):
            return Reply(404, b"not found", "text/plain")
        if full.is_dir():
            full = full / "index.html"
        if not full.is_file():
            return Reply(404, b"not found", "text/plain")
        ctype = TYPES.get(full.suffix.lower()) or (
            mimetypes.guess_type(full.name)[0] or "application/octet-stream"
        )
        return Reply(200, full.read_bytes(), ctype)

    async def api(self, method: str, name: str, q: dict[str, list[str]], body: bytes) -> Reply:
        arg = {k: v[0] for k, v in q.items()}
        if name in ("box/persona", "box/persona/state"):
            return self.persona(method, name.endswith("/state"), body)
        if name == "box/prefs":
            return self.prefs_route(method, body)
        if name == "box/cal":
            cal = self.cal if arg.get("id") == ROBOT else None
            return Reply.json({"cal": cal})
        if name == "box/skills":
            return Reply.json({"skills": self.skills})
        if name == "box/policy":
            path = self.policies.get(arg.get("id", ""))
            if path is None:
                return Reply.json({"error": "no such policy"}, 404)
            return Reply(200, path.read_bytes())
        if name == "box/memories":
            return Reply.json({"records": [], "version": 0})
        if name.startswith("box/brain/"):
            return Reply.json({"hits": [], "ok": True})
        if name in ("box/dream", "box/bodytest"):
            return Reply.json({"ok": True})
        if name == "config":
            return Reply.json(
                {
                    "fast": "sim",
                    "active": "sim",
                    "dream": "sim",
                    "fallback": "sim",
                    "wall": "",
                    "raw": 0,
                }
            )
        if name == "models":
            row = {
                "id": "sim",
                "name": "sim",
                "vision": True,
                "callEst": 0,
                "intel": None,
                "reasoning": False,
                "mandatory": False,
                "efforts": [],
            }
            return Reply.json({"simple": [row], "all": [row]})
        if name == "device":
            return Reply.json(
                {
                    "ok": True,
                    "token": "box",
                    "did": "sim",
                    "hasPass": True,
                    "balanceUsd": 0,
                    "spentUsd": 0,
                    "freeLeftUsd": 0,
                    "freeLifetimeUsd": 0,
                    "totalPurchased": 0,
                }
            )
        if name in ("journal", "episodes", "gallery", "ev", "hit", "feedback"):
            return Reply(204)
        if name == "fish-tts":
            return Reply(200, silent_wav(), "audio/wav")
        if name in ("llm", "llm-stream"):
            if arg.get("target") in ("groq-audio", "deepinfra-audio"):
                # Speech reaches the page as heard text (run.py), so a clip is only noise.
                return Reply.json({"text": ""})
            return await self.llm(body, stream=name == "llm-stream")
        return Reply.json({"error": "not found"}, 404)

    # ------------------------------------------------------------------ the persona

    def persona(self, method: str, state: bool, body: bytes) -> Reply:
        if self.soul is None:
            return Reply(204)  # a fresh creature on the phone, as a G2 session is
        if method == "GET":
            out: dict[str, Any] = {
                "persona": PERSONA,
                "version": self.version,
                "wearing": ROBOT,
                "brain": False,
                # A persona's own default (personas.py): his quiet moments invite him to go
                # exploring. False here gave e1 the old sit-still words and four-minute gaps.
                "explore": True,
            }
            if not state:
                out["soul"] = self.soul
            return Reply.json(out)
        msg = json.loads(body or b"{}")
        if msg.get("persona") != PERSONA:
            return Reply.json({"error": "other"}, 409)
        # Kept for the run only: the soul as Dave leaves it is part of what a run shows.
        self.soul = msg.get("soul") or self.soul
        self.version += 1
        return Reply.json({"version": self.version, "persona": PERSONA})

    def prefs_route(self, method: str, body: bytes) -> Reply:
        if self.soul is None:
            return Reply(204)
        if method == "POST":
            msg = json.loads(body or b"{}")
            for k, v in (msg.get("set") or {}).items():
                self.prefs[k] = v
            for k in msg.get("drop") or []:
                self.prefs.pop(k, None)
        return Reply.json({"prefs": dict(self.prefs), "version": 1, "persona": PERSONA})

    # ------------------------------------------------------------------ the model

    async def llm(self, body: bytes, stream: bool) -> Reply:
        req = json.loads(body or b"{}")
        call: dict[str, Any] = {"request": req, "t": self._t()}
        self.calls.append(call)
        try:
            text = await self.model(req)
        except Exception as e:  # the page sees what the box says when its model fails
            call["error"] = str(e)
            return Reply.json({"error": {"message": str(e)[:200]}}, 502)
        call["reply"] = text
        if not stream:
            msg = {"role": "assistant", "content": text}
            out = Reply.json({"choices": [{"index": 0, "finish_reason": "stop", "message": msg}]})
            out.call = call
            return out
        chunk = {"choices": [{"index": 0, "delta": {"content": text}}]}
        done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        sse = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\ndata: [DONE]\n\n"
        return Reply(200, sse.encode(), "text/event-stream", call)
