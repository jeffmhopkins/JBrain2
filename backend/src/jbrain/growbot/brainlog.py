"""The brain log: one zip of a window of the creature's life, for upstream's developer.

docs/plans/GROWBOT_PLAN.md §2 "Brain log". The owner asked (2026-11-12) for a file he can hand
the GrowBot developer: the last 1, 3 or 6 hours of every model turn, transcription and spoken
line from the call log (`calls.py`), the creature's own diary (`journal.py`), the latency
summary, and the box's state, in one download.

It leaves the house, so it carries no ids or secrets of the box's: personas, robots and diaries
are numbered in order of appearance, robot codes and persona ids are swapped for those numbers
wherever they appear, and anything shaped like a bearer token is cut. Redacted (the default)
follows upstream's own diary rule, so the developer reads it the way they read their own logs:
a quoted span of three or more characters becomes `«h8·Nw»` with upstream's djb2 `h8`, which
lets a hashed line here be joined to the same hashed line in the diary; a transcript is hashed
whole; the creature's own words survive. Names the owner lists are replaced everywhere, in
either mode, because a soul and the creature's lines can name the people it lives with.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from jbrain.db.session import SessionContext
from jbrain.growbot.calls import CallRecord, LoggedCall
from jbrain.growbot.journal import DiaryRow
from jbrain.growbot.personas import Persona
from jbrain.growbot.repo import RobotInfo

HOURS: Final = (1, 3, 6)
NAMES_MAX: Final = 20
# A system prompt this long is written once under prompts/ and referenced, since the creature
# sends the same soul with every turn and a window would otherwise repeat it thousands of times.
PROMPT_SHARE_MIN: Final = 512
_PAGE: Final = 200
# Bounds the zip, not a window a creature fills: six hours at a turn a second is 21,600.
CALLS_MAX: Final = 30_000
DIARY_MAX: Final = 60_000

_QUOTE_RES: Final = (re.compile(r'"([^"\n]{3,})"'), re.compile(r"“([^”\n]{3,})”"))
_SECRET_RE: Final = re.compile(
    r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}|\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"
)
_CODE_RE: Final = re.compile(r"\bgb-[A-Za-z0-9]{1,32}\b")
_UUID_RE: Final = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


class CallPages(Protocol):
    async def page(
        self, ctx: SessionContext, *, since: datetime, after_id: int = 0, limit: int = 200
    ) -> list[LoggedCall]: ...


class DiaryPages(Protocol):
    async def page(
        self, ctx: SessionContext, *, since: datetime, after_id: int = 0, limit: int = 500
    ) -> list[DiaryRow]: ...


def h8(s: str) -> str:
    """Upstream's `h8` (next.html): djb2 over UTF-16 code units, eight hex digits."""
    h = 5381
    data = s.encode("utf-16-le")
    for i in range(0, len(data), 2):
        h = ((h << 5) + h + (data[i] | data[i + 1] << 8)) & 0xFFFFFFFF
    return f"{h:08x}"


def hashed(s: str) -> str:
    return f"«{h8(s)}·{len(s.split())}w»"


def redact_quotes(s: str) -> str:
    """Upstream's `redactQ` without its name scrub (the owner's list does that here)."""
    for pattern in _QUOTE_RES:
        s = pattern.sub(lambda m: hashed(m.group(1)), s)
    return s


@dataclass
class Scrub:
    """What every string in the zip passes through, and the numbering it builds as it goes."""

    redact: bool
    names: list[str] = field(default_factory=list)
    personas: dict[str, str] = field(default_factory=dict)
    robots: dict[str, str] = field(default_factory=dict)
    diaries: dict[str, str] = field(default_factory=dict)
    _names_re: re.Pattern[str] | None = None

    def __post_init__(self) -> None:
        names = sorted({n.strip() for n in self.names if len(n.strip()) >= 2}, key=len)
        self.names = names
        if names:
            alts = "|".join(re.escape(n) for n in reversed(names))
            self._names_re = re.compile(rf"(?i)(?<!\w)(?:{alts})(?!\w)")

    def persona(self, pid: str | None) -> str | None:
        if not pid:
            return None
        return self.personas.setdefault(pid.lower(), f"persona-{len(self.personas) + 1}")

    def robot(self, code: str) -> str:
        if not code:
            return ""
        return self.robots.setdefault(code, f"robot-{len(self.robots) + 1}")

    def diary(self, sid: str) -> str:
        return self.diaries.setdefault(sid, f"diary-{len(self.diaries) + 1}")

    def text(self, s: str, *, quotes: bool = True) -> str:
        """Ids swapped, secrets cut, quoted speech hashed (redacted), listed names replaced."""
        s = _SECRET_RE.sub(lambda m: (m.group(1) or "") + "[secret]", s)
        s = _CODE_RE.sub(lambda m: self.robot(m.group(0)), s)
        s = _UUID_RE.sub(lambda m: self.persona(m.group(0)) or "", s)
        if self.redact and quotes:
            s = redact_quotes(s)
        if self._names_re is not None:
            s = self._names_re.sub("[name]", s)
        return s

    def walk(self, value: Any, *, quotes: bool = True) -> Any:
        if isinstance(value, str):
            return self.text(value, quotes=quotes)
        if isinstance(value, list):
            return [self.walk(v, quotes=quotes) for v in value]
        if isinstance(value, dict):
            return {
                self.text(str(k), quotes=False): self.walk(v, quotes=quotes)
                for k, v in value.items()
            }
        return value

    def heard(self, s: str) -> str:
        """A transcript: everything it says is someone in the house, so hashed whole."""
        if not self.redact or not s.strip():
            return self.text(s)
        return hashed(s.strip())


class _Prompts:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def share(self, prompt: str) -> str:
        key = hashlib.sha256(prompt.encode()).hexdigest()[:12]
        self.files.setdefault(key, prompt)
        return f"prompts/{key}.txt"


def _content(content: Any, scrub: Scrub, prompts: _Prompts | None) -> Any:
    if isinstance(content, str):
        out = scrub.text(content)
        if prompts is not None and len(out) >= PROMPT_SHARE_MIN:
            return {"shared": prompts.share(out)}
        return out
    return scrub.walk(content)


def _request(request: dict[str, Any], scrub: Scrub, prompts: _Prompts) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in request.items():
        if key != "messages" or not isinstance(value, list):
            out[key] = scrub.walk(value)
            continue
        messages = []
        for message in value:
            if not isinstance(message, dict):
                messages.append(scrub.walk(message))
                continue
            role = message.get("role")
            shown = {k: scrub.walk(v) for k, v in message.items() if k != "content"}
            shown["content"] = _content(
                message.get("content"), scrub, prompts if role == "system" else None
            )
            messages.append(shown)
        out["messages"] = messages
    return out


def call_row(call: LoggedCall, scrub: Scrub, prompts: _Prompts) -> dict[str, Any]:
    rec = call.record
    if rec.kind == "llm":
        request = _request(rec.request, scrub, prompts)
        reply = scrub.text(rec.reply)
    elif rec.kind == "stt":
        request = {
            k: (scrub.heard(v) if k == "prompt" and isinstance(v, str) else scrub.walk(v))
            for k, v in rec.request.items()
        }
        reply = scrub.heard(rec.reply)
    else:
        request = scrub.walk(rec.request)
        reply = scrub.text(rec.reply)
    return {
        "id": call.id,
        "at": call.created_at.isoformat(),
        "kind": rec.kind,
        "ok": rec.ok,
        "persona": scrub.persona(rec.persona_id),
        "robot": scrub.robot(rec.robot),
        "model": rec.model,
        "error": scrub.text(rec.error, quotes=False),
        "total_ms": rec.total_ms,
        "timings": rec.timings,
        "images": rec.images,
        "image_bytes": rec.image_bytes,
        "prompt_tokens": rec.prompt_tokens,
        "cached_tokens": rec.cached_tokens,
        "completion_tokens": rec.completion_tokens,
        "request": request,
        "reply": reply,
        "reasoning": scrub.text(rec.reasoning),
    }


def _head(head: dict[str, Any], scrub: Scrub) -> dict[str, Any]:
    boot = head.get("boot")
    # `hh` is a hash of the box's hostname: nothing upstream can use, and it names the box.
    if isinstance(boot, dict):
        head = {**head, "boot": {k: v for k, v in boot.items() if k != "hh"}}
    return scrub.walk(head, quotes=False)


def diary_row(row: DiaryRow, scrub: Scrub) -> dict[str, Any]:
    boot = row.head.get("boot")
    return {
        "diary": scrub.diary(row.sid),
        "persona": scrub.persona(row.persona_id),
        "robot": scrub.robot(row.robot),
        "received": row.created_at.isoformat(),
        "boot": boot.get("t") if isinstance(boot, dict) else None,
        **scrub.walk(row.row),
    }


def _line(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _readme(manifest: dict[str, Any]) -> str:
    mode = (
        "Redacted, the way upstream's own diary is: a quoted span of three or more characters "
        "is `«h8·Nw»` (upstream's djb2 h8 of the span, and its word count), and a transcription "
        "is hashed whole. The creature's own words are kept."
        if manifest["redacted"]
        else "Not redacted: what people said is here as it was heard."
    )
    return f"""# GrowBot brain log

A window of one GrowBot creature's life on a self-hosted box (JBrain), shared for debugging.
The creature is upstream's app pinned at the commit in `growbot/UPSTREAM` of JBrain
`{manifest["jbrain"]}`, served by the box, with its model, speech and voice answered on the
box instead of growbot.dev.

- Window: {manifest["from"]} to {manifest["to"]} ({manifest["hours"]} h, UTC).
- {mode}
- Personas, robots and diaries are numbered (`persona-1`, `robot-1`, `diary-1`); the box's ids,
  codes and keys are not in this file. Names the owner listed read `[name]`.
- Camera frames are never kept: a turn with pictures says how many and their base64 size.

## Files

- `calls.jsonl`: every model turn (`llm`), transcription (`stt`) and spoken line (`tts`),
  oldest first. `request` is the chat body as the box's model received it, `reply` and
  `reasoning` what it streamed back. `timings` is where the time went, in ms: `lock_ms` (wait
  for the box's one model lane), `admit_ms`, `first_byte_ms`, `first_token_ms`,
  `upstream_ms`, and the engine's own `engine_prefill_ms` / `engine_decode_ms` and tokens per
  second. A long system prompt is written once under `prompts/` and referenced as
  `{{"shared": "prompts/<hash>.txt"}}`.
- `summary.json`: latency per kind and stage (n, p50, p90, max), model turns with a picture
  apart from text-only ones.
- `diary.jsonl`: the creature's own diary rows (what it posts to `/api/journal`), oldest first,
  each once, with the diary and page load (`boot`) it came from. `diary-pages.json` holds each
  page load's header (build hash, deploy, mode, user agent).
- `box.json`: the personas and robots in the window, and which models answered.
"""


async def build(
    *,
    ctx: SessionContext,
    calls: CallPages,
    journal: DiaryPages | None,
    personas: list[Persona],
    robots: list[RobotInfo],
    summarize: Callable[[list[LoggedCall]], dict[str, Any]],
    hours: int,
    redact: bool,
    names: list[str],
    now: datetime,
    jbrain: str,
) -> tuple[bytes, str]:
    """The zip and its file name."""
    since = now - timedelta(hours=hours)
    scrub = Scrub(redact=redact, names=names[:NAMES_MAX])
    prompts = _Prompts()
    stamp = now.strftime("%Y%m%d-%H%M")
    root = f"growbot-brain-log-{stamp}-{hours}h"
    buf = io.BytesIO()
    models: set[str] = set()
    counts = {"llm": 0, "stt": 0, "tts": 0, "diary": 0}
    pages: dict[str, dict[str, Any]] = {}
    # Summary needs only timings and sizes, never the prompt, so it keeps a stripped copy.
    light: list[LoggedCall] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        with zf.open(f"{root}/calls.jsonl", "w") as out:
            after = 0
            while sum(counts.values()) < CALLS_MAX:
                batch = await calls.page(ctx, since=since, after_id=after, limit=_PAGE)
                for call in batch:
                    out.write(_line(call_row(call, scrub, prompts)))
                    counts[call.record.kind] = counts.get(call.record.kind, 0) + 1
                    if call.record.model:
                        models.add(call.record.model)
                    rec = call.record
                    light.append(
                        LoggedCall(
                            call.id,
                            call.created_at,
                            CallRecord(
                                kind=rec.kind,
                                ok=rec.ok,
                                total_ms=rec.total_ms,
                                images=rec.images,
                                image_bytes=rec.image_bytes,
                                prompt_tokens=rec.prompt_tokens,
                                cached_tokens=rec.cached_tokens,
                                timings=rec.timings,
                            ),
                        )
                    )
                if len(batch) < _PAGE:
                    break
                after = batch[-1].id
        with zf.open(f"{root}/diary.jsonl", "w") as out:
            after = 0
            while journal is not None and counts["diary"] < DIARY_MAX:
                rows = await journal.page(ctx, since=since, after_id=after, limit=_PAGE * 5)
                for row in rows:
                    out.write(_line(diary_row(row, scrub)))
                    counts["diary"] += 1
                    boot = row.head.get("boot")
                    key = (
                        f"{scrub.diary(row.sid)}@{boot.get('t') if isinstance(boot, dict) else ''}"
                    )
                    pages.setdefault(key, _head(row.head, scrub))
                if len(rows) < _PAGE * 5:
                    break
                after = rows[-1].id
        for key, prompt in prompts.files.items():
            zf.writestr(f"{root}/prompts/{key}.txt", prompt)
        zf.writestr(f"{root}/summary.json", json.dumps(summarize(light), indent=1))
        zf.writestr(f"{root}/diary-pages.json", json.dumps(pages, indent=1, ensure_ascii=False))
        box = {
            "personas": [
                {
                    "persona": scrub.persona(p.id),
                    "face": p.face,
                    "soul_version": p.version,
                    "memories": p.memories,
                    "checkpoints": p.checkpoints,
                    "wearing": scrub.robot(p.wearing) if p.wearing else None,
                }
                for p in personas
                if p.id.lower() in scrub.personas
            ],
            "robots": [
                {
                    "robot": scrub.robot(r.code),
                    "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
                    "key_revoked": r.revoked_at is not None,
                }
                for r in robots
                if r.code in scrub.robots
            ],
            "models": sorted(models),
        }
        zf.writestr(f"{root}/box.json", json.dumps(box, indent=1))
        manifest = {
            "from": since.isoformat(),
            "to": now.isoformat(),
            "hours": hours,
            "redacted": redact,
            "names_hidden": len(scrub.names),
            "jbrain": jbrain,
            "counts": counts,
            "prompts": len(prompts.files),
        }
        zf.writestr(f"{root}/manifest.json", json.dumps(manifest, indent=1))
        zf.writestr(f"{root}/README.md", _readme(manifest))
    return buf.getvalue(), f"{root}.zip"
