"""The GrowBot call log (migration 0260): every model turn, transcription and voice clip.

The model lane (`api/growbot_llm.py`) writes an 'llm' row per turn with the request as it went
to the engine and the reply it streamed back; the sidecar posts 'stt' and 'tts' rows. The
point is latency analysis from the debug API, so each row carries per-stage timings and the
engine's own prefill/decode split, and pictures are kept as their size, never their pixels:
the question is how long a frame cost, and a week of camera frames would be gigabytes of the
house on disk for no answer the size does not give.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

KEEP: Final = timedelta(days=7)
# The writer prunes on every Nth insert rather than each one: a turn a second would otherwise
# pay a range delete per turn for rows that age out once a day. The first write after a
# start prunes too, so a box that restarts often still sheds its old rows.
_PRUNE_EVERY: Final = 200
# A runaway reply (a model stuck repeating) is cut here, so one turn cannot bloat the table.
TEXT_MAX: Final = 64 * 1024
KINDS: Final = ("llm", "stt", "tts")


@dataclass
class CallRecord:
    kind: str
    ok: bool
    total_ms: int
    persona_id: str | None = None
    robot: str = ""
    model: str = ""
    error: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    reply: str = ""
    reasoning: str = ""
    images: int = 0
    image_bytes: int = 0
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None
    timings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoggedCall:
    id: int
    created_at: datetime
    record: CallRecord


def _clip(value: str) -> str:
    return value if len(value) <= TEXT_MAX else value[:TEXT_MAX] + "…[cut]"


def redact_images(payload: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    """The request as logged: every inline picture replaced by its media type and size.
    Returns it with the picture count and their base64 length in total."""
    out = copy.deepcopy(payload)
    count = size = 0
    for message in out.get("messages") or ():
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for i, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            url = url if isinstance(url, str) else ""
            head, _, data = url.partition(",")
            count += 1
            size += len(data)
            content[i] = {
                "type": "image",
                "media": head.removeprefix("data:").removesuffix(";base64"),
                "b64_chars": len(data),
            }
    return out, count, size


def _int(value: object) -> int | None:
    return value if type(value) is int else None


class ReplyCollector:
    """Rebuilds the reply from what the engine sent back, an SSE stream or one JSON body, and
    notes when the first word arrived. Fed the raw bytes as they pass through."""

    def __init__(self, stream: bool, started: float) -> None:
        self._stream = stream
        self._started = started
        self._buf = b""
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._tools: list[str] = []
        self.first_token_ms: int | None = None
        self.usage: dict[str, Any] = {}
        self.engine: dict[str, Any] = {}
        self.error = ""

    def feed(self, chunk: bytes) -> None:
        if not self._stream:
            self._buf += chunk
            return
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data and data != b"[DONE]":
                self._take(data, delta=True)

    def _take(self, data: bytes, *, delta: bool) -> None:
        try:
            obj = json.loads(data)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("error"), dict):
            self.error = str(obj["error"].get("message") or obj["error"])[:500]
        if isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]
        if isinstance(obj.get("timings"), dict):
            self.engine = obj["timings"]
        for choice in obj.get("choices") or ():
            part = choice.get("delta" if delta else "message") if isinstance(choice, dict) else None
            if not isinstance(part, dict):
                continue
            words = part.get("content")
            thought = part.get("reasoning_content")
            if (words or thought) and self.first_token_ms is None:
                self.first_token_ms = round((time.monotonic() - self._started) * 1000)
            if isinstance(words, str):
                self._content.append(words)
            if isinstance(thought, str):
                self._reasoning.append(thought)
            for call in part.get("tool_calls") or ():
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict):
                    self._tools.append(f"{fn.get('name') or ''}{fn.get('arguments') or ''}")

    def finish(self) -> None:
        if self._stream:
            # A last event the engine sent without its closing newline.
            self.feed(b"\n")
        elif self._buf:
            self._take(self._buf, delta=False)
        self._buf = b""

    @property
    def reply(self) -> str:
        words = "".join(self._content)
        if self._tools:
            words += "\n[tool] " + "".join(self._tools)
        return _clip(words)

    @property
    def reasoning(self) -> str:
        return _clip("".join(self._reasoning))

    def fill(self, rec: CallRecord) -> None:
        """Put the reply, its tokens and the engine's own timings on `rec`."""
        rec.reply = self.reply
        rec.reasoning = self.reasoning
        details = self.usage.get("prompt_tokens_details")
        rec.prompt_tokens = _int(self.usage.get("prompt_tokens")) or _int(
            self.engine.get("prompt_n")
        )
        rec.completion_tokens = _int(self.usage.get("completion_tokens")) or _int(
            self.engine.get("predicted_n")
        )
        rec.cached_tokens = _int(self.engine.get("cache_n"))
        if rec.cached_tokens is None and isinstance(details, dict):
            rec.cached_tokens = _int(details.get("cached_tokens"))
        if self.first_token_ms is not None:
            rec.timings["first_token_ms"] = self.first_token_ms
        for ours, theirs in (
            ("engine_prefill_ms", "prompt_ms"),
            ("engine_decode_ms", "predicted_ms"),
            ("engine_prefill_tps", "prompt_per_second"),
            ("engine_decode_tps", "predicted_per_second"),
        ):
            value = self.engine.get(theirs)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rec.timings[ours] = round(float(value), 1)
        if self.error and not rec.error:
            rec.error = self.error
            rec.ok = False


# Characters per token of the creature's system message, near enough to tell a turn that
# resumed after the system message from one that re-read it (measured ~4.0 on Flash-Next).
_CHARS_PER_TOKEN: Final = 4


def is_cold(rec: CallRecord) -> bool:
    """A model turn that re-read its system message instead of resuming after it."""
    chars = rec.timings.get("system_chars")
    if rec.cached_tokens is None or not isinstance(chars, int) or chars <= 0:
        return False
    return rec.cached_tokens * _CHARS_PER_TOKEN < chars // 2


class CallLog(Protocol):
    async def record(self, ctx: SessionContext, rec: CallRecord) -> None: ...

    async def recent(
        self,
        ctx: SessionContext,
        *,
        since: datetime | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[LoggedCall]: ...

    async def page(
        self, ctx: SessionContext, *, since: datetime, after_id: int = 0, limit: int = 200
    ) -> list[LoggedCall]: ...


_COLUMNS: Final = (
    "kind, persona_id, robot, model, ok, error, request, reply, reasoning, images, image_bytes,"
    " prompt_tokens, cached_tokens, completion_tokens, total_ms, timings"
)

_SELECT: Final = _COLUMNS.replace("persona_id", "persona_id::text AS persona_id")


class SqlCallLog:
    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker
        self._writes = 0

    async def record(self, ctx: SessionContext, rec: CallRecord) -> None:
        if rec.kind not in KINDS:
            raise ValueError("unknown call kind")
        self._writes += 1
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    f"INSERT INTO app.growbot_calls ({_COLUMNS}) VALUES (:kind,"
                    " CAST(:persona AS uuid), :robot, :model, :ok, :error, CAST(:request AS jsonb),"
                    " :reply, :reasoning, :images, :image_bytes, :prompt_tokens, :cached_tokens,"
                    " :completion_tokens, :total_ms, CAST(:timings AS jsonb))"
                ),
                {
                    "kind": rec.kind,
                    "persona": rec.persona_id or None,
                    "robot": rec.robot,
                    "model": rec.model,
                    "ok": rec.ok,
                    "error": rec.error[:2000],
                    "request": json.dumps(rec.request),
                    "reply": _clip(rec.reply),
                    "reasoning": _clip(rec.reasoning),
                    "images": rec.images,
                    "image_bytes": rec.image_bytes,
                    "prompt_tokens": rec.prompt_tokens,
                    "cached_tokens": rec.cached_tokens,
                    "completion_tokens": rec.completion_tokens,
                    "total_ms": rec.total_ms,
                    "timings": json.dumps(rec.timings),
                },
            )
            if (self._writes - 1) % _PRUNE_EVERY == 0:
                await session.execute(
                    text(
                        "DELETE FROM app.growbot_calls"
                        " WHERE created_at < now() - make_interval(secs => :keep)"
                    ),
                    {"keep": KEEP.total_seconds()},
                )

    async def recent(
        self,
        ctx: SessionContext,
        *,
        since: datetime | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[LoggedCall]:
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT id, created_at, {_SELECT}"
                        " FROM app.growbot_calls"
                        " WHERE (CAST(:since AS timestamptz) IS NULL"
                        "        OR created_at >= CAST(:since AS timestamptz))"
                        "   AND (CAST(:kind AS text) IS NULL OR kind = CAST(:kind AS text))"
                        " ORDER BY id DESC LIMIT :limit"
                    ),
                    {"since": since, "kind": kind, "limit": limit},
                )
            ).mappings()
            return [_logged(row) for row in rows]

    async def page(
        self, ctx: SessionContext, *, since: datetime, after_id: int = 0, limit: int = 200
    ) -> list[LoggedCall]:
        """Rows since `since`, oldest first, after row `after_id`: an export walks a window
        a page at a time, so six hours of prompts never sit in memory at once."""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT id, created_at, {_SELECT} FROM app.growbot_calls"
                        " WHERE created_at >= :since AND id > :after ORDER BY id LIMIT :limit"
                    ),
                    {"since": since, "after": after_id, "limit": limit},
                )
            ).mappings()
            return [_logged(row) for row in rows]


def _logged(row: Any) -> LoggedCall:
    return LoggedCall(
        id=row["id"],
        created_at=row["created_at"],
        record=CallRecord(
            kind=row["kind"],
            ok=row["ok"],
            total_ms=row["total_ms"],
            persona_id=row["persona_id"],
            robot=row["robot"],
            model=row["model"],
            error=row["error"],
            request=row["request"],
            reply=row["reply"],
            reasoning=row["reasoning"],
            images=row["images"],
            image_bytes=row["image_bytes"],
            prompt_tokens=row["prompt_tokens"],
            cached_tokens=row["cached_tokens"],
            completion_tokens=row["completion_tokens"],
            timings=row["timings"],
        ),
    )
