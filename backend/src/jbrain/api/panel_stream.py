"""The desk panel's turn as a stream: JT2 (firmware-c6 0.0.12).

JT1 (`endpoint.jerv_frame`) is one frame at the end of the turn: what was heard, the answer
condensed to 240 bytes, the speech. A tool-using turn can run for minutes, and for all of them
the panel could say only "Transcribing…" — then, at 100 s, "the answer will be in the app"
(owner, 2026-10-12: "I want better updates for tool use … to come through the panel and not to
have it direct me to my app"). JT2 is the same turn sent as it happens.

THE FORMAT. `JT2`, then records until the body ends. Every record is

    type: u8 | length: u16 little-endian | payload: `length` bytes

    0x01 HEARD   UTF-8: what the box heard, as soon as Whisper has it
    0x02 STATUS  UTF-8, at most STATUS_BYTES: what the agent is doing now ("Searching the web…")
    0x03 ALIVE   empty: nothing new, the turn is still going — at least every ALIVE_S
    0x04 ANSWER  flags: u8 (bit 0 speech follows, bit 1 text was cut to fit) | UTF-8 answer
    0x05 PCM     16 kHz mono s16le speech, at most PCM_RECORD bytes, an even count
    0x06 END     code: u8 (0 answered, 1 nothing heard, 2 failed) | UTF-8 why

A reader skips a type it does not know (its length says how far) and may refuse a record longer
than MAX_RECORD. A stream that ends without END was cut off. The panel asks for JT2 with
`jt=2`; a panel that does not keeps JT1, and the twins' pets are never offered either.

THE SPEECH IS THE WHOLE ANSWER, sentence by sentence (owner, 2026-10-12: "longer replies have a
way to read longer than 30 seconds"): each group of sentences is synthesised while the one
before it is sent, and sent as PCM records as soon as it is ready. The panel plays from a 16 KB
ring and reads no faster than its speaker drains it, so the box is paced by TCP, never holds
more than two groups of audio, and stops synthesising the moment the panel goes away. Capped at
SPEECH_CAP_S of audio. The text on the panel is the whole answer up to TEXT_BYTES.

NOTHING HERE OWNS THE TURN. The agent turn is the PWA's detached `/chat` task; this only follows
its live frames. A panel that disconnects — BOOT, a stop, a dropped radio — cancels this
generator and nothing else: the turn finishes into the thread as it would have.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import struct
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from jbrain.api import panel_agent
from jbrain.api.panel_labels import ING, STEP_LABELS

log = structlog.get_logger()

MAGIC = b"JT2"
HEARD, STATUS, ALIVE, ANSWER, PCM, END = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
ANSWER_SPEECH, ANSWER_CUT = 0x01, 0x02
END_OK, END_NOTHING, END_FAILED = 0, 1, 2

#: The largest record either side sends. The panel refuses anything longer.
MAX_RECORD = 4096
#: 128 ms of speech: small enough that the panel's 16 KB ring always has room for a whole one.
PCM_RECORD = 4096
#: The status line under the transcript is one line on the panel.
STATUS_BYTES = 60
#: The panel keeps the newest agent answer up to this many bytes (firmware `MSG_LONG_TEXT`).
TEXT_BYTES = 1000
#: Something every this many seconds, so neither side's idle timeout fires (the panel's is 45 s).
ALIVE_S = 8.0
#: Five minutes of audio, the most one answer is read aloud for.
SPEECH_CAP_S = 300
PANEL_RATE = 16000
#: The first group is one sentence, so the voice starts quickly; later groups are longer, so
#: there are fewer seams between them. One synthesis call speaks at most 600 characters
#: (`endpoint._panel_speech`), so no group is longer than that.
GROUP_CHARS = 400
SENTENCE_CHARS = 500

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def record(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_RECORD:
        raise ValueError(f"record of {len(payload)} bytes")
    return struct.pack("<BH", kind, len(payload)) + payload


def cut_utf8(text: str, limit: int) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return raw
    return raw[:limit].decode("utf-8", "ignore").encode("utf-8")


def text_record(kind: int, text: str, limit: int = MAX_RECORD) -> bytes:
    return record(kind, cut_utf8(text, limit))


def status_record(text: str) -> bytes:
    raw = text.encode("utf-8")
    if len(raw) > STATUS_BYTES:
        room = STATUS_BYTES - len("…".encode())
        text = raw[:room].decode("utf-8", "ignore").rstrip(" :,") + "…"
    return text_record(STATUS, text, STATUS_BYTES)


def end_record(code: int, why: str = "") -> bytes:
    return record(END, bytes([code]) + cut_utf8(why, 200))


def answer_record(text: str, *, speech: bool, cut: bool) -> bytes:
    flags = (ANSWER_SPEECH if speech else 0) | (ANSWER_CUT if cut else 0)
    return record(ANSWER, bytes([flags]) + cut_utf8(text, MAX_RECORD - 1))


def pcm_records(pcm: bytes) -> list[bytes]:
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    return [record(PCM, pcm[i : i + PCM_RECORD]) for i in range(0, len(pcm), PCM_RECORD)]


def whole(heard: str, reply: str, pcm: bytes) -> bytes:
    """A turn that was already finished when the response began (the tool-less persona): the
    same records a live one ends with."""
    text, cut = panel_agent.screen_text(reply, TEXT_BYTES)
    return b"".join(
        [
            MAGIC,
            text_record(HEARD, heard),
            answer_record(text, speech=bool(pcm), cut=cut),
            *pcm_records(pcm),
            end_record(END_OK),
        ]
    )


# ---- what the agent is doing, in words --------------------------------------------------------

#: The argument that names a tool's target, shown after its label ("Searching the web: tides").
_TARGET_KEYS = ("query", "goal", "url", "place", "location", "name", "title")


def progress_label(name: str, args: dict[str, Any] | None = None) -> str:
    """The PWA's step label for `name`, in the present, with the call's target when it has one:
    `web_search` + {"query": "high tide Port St John"} -> "Searching the web: high tide Port St
    John…". Every label is at most STATUS_BYTES once sent (`status_record`)."""
    past = STEP_LABELS.get(name)
    if past is None and name.startswith("lookup_"):
        past = f"Checked {name[7:].replace('_', ' ')}"
    if past is None:
        base = "Working"
    else:
        verb, _, rest = past.partition(" ")
        base = f"{ING.get(verb, verb)} {rest}".strip()
    target = ""
    for key in _TARGET_KEYS:
        value = (args or {}).get(key)
        if isinstance(value, str) and value.strip():
            target = value.strip()
            if key == "url":
                target = re.sub(r"^https?://(www\.)?", "", target).split("/", 1)[0]
            break
    target = re.sub(r"\s+", " ", target)
    return f"{base}: {target}…" if target else f"{base}…"


THINKING = "Thinking…"
WRITING = "Writing the answer…"


@dataclass
class Progress:
    """Folds the agent's live SSE frames into status lines, saying each only when it changes."""

    said: str = ""
    tools: int = 0
    _wrote: bool = False
    _thinking_after_tool: bool = False
    lines: list[str] = field(default_factory=list)

    def feed(self, frame: bytes) -> str | None:
        if not frame.startswith(b"data:"):
            return None
        try:
            event = json.loads(frame[5:].strip())
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        line: str | None = None
        if kind == "tool_call":
            self.tools += 1
            args = event.get("arguments")
            line = progress_label(
                str(event.get("name", "")), args if isinstance(args, dict) else {}
            )
            self._thinking_after_tool = True
            self._wrote = False
        elif kind == "tool_progress" and isinstance(event.get("label"), str) and event["label"]:
            line = str(event["label"])
            if not line.endswith("…"):
                line += "…"
        elif kind == "reasoning_delta" and self._thinking_after_tool:
            self._thinking_after_tool = False
            line = THINKING
        elif kind == "text_delta" and not self._wrote:
            self._wrote = True
            line = WRITING
        if line is None or line == self.said:
            return None
        self.said = line
        self.lines.append(line)
        return line


# ---- the speech, a group of sentences at a time ---------------------------------------------


def speech_groups(text: str) -> list[str]:
    """`text` as the groups it is synthesised in: the first sentence alone (so the voice starts
    soon after the words), then whole sentences up to GROUP_CHARS. A sentence longer than
    SENTENCE_CHARS is split on words, because one synthesis call speaks at most that much."""
    sentences: list[str] = []
    for sentence in (x.strip() for x in _SENTENCE.split(text)):
        while len(sentence) > SENTENCE_CHARS:
            head = sentence[:SENTENCE_CHARS]
            head = head.rsplit(" ", 1)[0] if " " in head else head
            sentences.append(head)
            sentence = sentence[len(head) :].strip()
        if sentence:
            sentences.append(sentence)
    if not sentences:
        return []
    groups = [sentences[0]]
    cur = ""
    for sentence in sentences[1:]:
        if cur and len(cur) + 1 + len(sentence) > GROUP_CHARS:
            groups.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    if cur:
        groups.append(cur)
    return groups


Synth = Callable[[str, int], Awaitable[bytes]]


async def speech(text: str, synth: Synth, stats: dict[str, Any]) -> AsyncGenerator[bytes, None]:
    """PCM records for all of `text`, one group synthesised ahead of the one being sent; ALIVE
    while a slow group is still coming. Stops at SPEECH_CAP_S of audio. A group that cannot be
    spoken ends the speech, not the answer."""
    budget = int(SPEECH_CAP_S * PANEL_RATE * 2)
    groups = speech_groups(text)
    nxt: asyncio.Task[bytes] | None = None
    try:
        for i, _group in enumerate(groups):
            if budget <= 0:
                stats["speech_capped"] = True
                break
            task = nxt or asyncio.ensure_future(synth(groups[i], budget))
            nxt = None
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=ALIVE_S)
                if not done:
                    yield record(ALIVE)
            try:
                pcm = task.result()
            except Exception as exc:  # noqa: BLE001 — the words already went
                log.warning("panel_stream.speech_failed", group=i, error=repr(exc))
                stats["speech_failed"] = True
                break
            pcm = pcm[:budget]
            budget -= len(pcm)
            if i + 1 < len(groups) and budget > 0:
                nxt = asyncio.ensure_future(synth(groups[i + 1], budget))
            stats["speech_bytes"] = stats.get("speech_bytes", 0) + len(pcm)
            stats["groups"] = i + 1
            for rec in pcm_records(pcm):
                yield rec
    finally:
        # The panel went away (or the speech ended early): nothing more is synthesised for it.
        if nxt is not None and not nxt.done():
            nxt.cancel()
            with contextlib.suppress(BaseException):
                await nxt


# ---- the turn ---------------------------------------------------------------------------------


@dataclass
class Turn:
    """What the endpoint hands the stream: the steps it cannot take itself."""

    transcribe: Callable[[], Awaitable[str]]
    start: Callable[[str], Awaitable[panel_agent.Started]]
    synth: Callable[[panel_agent.Started], Awaitable[Synth | None]]
    agent: panel_agent.PanelAgent
    voice: bool
    cap_s: float = panel_agent.AGENT_STREAM_CAP_S
    stats: dict[str, Any] = field(default_factory=dict)


async def _alive_until(task: asyncio.Future[Any]) -> AsyncIterator[bytes]:
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=ALIVE_S)
        if not done:
            yield record(ALIVE)


async def _follow(
    started: panel_agent.Started, cap_s: float, stats: dict[str, Any]
) -> AsyncIterator[bytes]:
    """STATUS as the agent works, ALIVE when it is quiet, until the turn ends or `cap_s` passes.
    The live turn's frames are pumped into a queue by a task of our own, so a keep-alive wait
    never cancels the turn's own iterator."""
    live = started.live
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for frame in live.stream(0):
                queue.put_nowait(frame)
        finally:
            queue.put_nowait(None)

    pumper = asyncio.ensure_future(pump())
    progress = Progress()
    deadline = time.monotonic() + cap_s
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                stats["capped"] = True
                return
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=min(ALIVE_S, left))
            except TimeoutError:
                yield record(ALIVE)
                continue
            if frame is None:
                return
            line = progress.feed(frame)
            if line:
                yield status_record(line)
    finally:
        stats["statuses"] = len(progress.lines)
        stats["tools"] = progress.tools
        pumper.cancel()
        with contextlib.suppress(BaseException):
            await pumper


async def agent_turn(turn: Turn) -> AsyncGenerator[bytes, None]:
    """The whole JT2 response for one spoken agent turn. Every failure is a record, never a
    broken stream: the panel always has something to show."""
    stats = turn.stats
    t0 = time.monotonic()
    yield MAGIC
    stt = asyncio.ensure_future(turn.transcribe())
    async for rec in _alive_until(stt):
        yield rec
    try:
        heard = stt.result()
    except Exception as exc:  # noqa: BLE001 — "could not hear" is an answer too
        log.warning("panel_stream.stt_failed", error=repr(exc))
        yield end_record(END_FAILED, "the box could not hear")
        return
    stats["stt_ms"] = int((time.monotonic() - t0) * 1000)
    stats["heard"] = heard[:120]
    if not heard:
        yield end_record(END_NOTHING)
        return
    yield text_record(HEARD, heard)
    yield status_record(THINKING)

    starting = asyncio.ensure_future(turn.start(heard))
    async for rec in _alive_until(starting):
        yield rec
    try:
        started = starting.result()
    except Exception as exc:  # noqa: BLE001
        log.warning("panel_stream.start_failed", error=repr(exc))
        yield answer_record(panel_agent.FAILED, speech=False, cut=False)
        yield end_record(END_FAILED, "the agent did not start")
        return
    stats["session"] = started.session_id
    if started.early is not None:
        answer = started.early
    else:
        async for rec in _follow(started, turn.cap_s, stats):
            yield rec
        if started.live.task.done():
            answer = panel_agent.settle(started, turn.agent)
        else:
            answer = started.answer(panel_agent.STILL_WORKING, "still_running")
            answer = panel_agent.Answer(
                answer.words,
                answer.stop_reason,
                answer.session_id,
                answer.waited_ms,
                panel_agent.STILL_WORKING_STREAM,
            )
    stats["agent_ms"] = answer.waited_ms
    stats["stop_reason"] = answer.stop_reason
    full = answer.full or answer.words
    text, cut = panel_agent.screen_text(full, TEXT_BYTES)
    synth = await turn.synth(started) if turn.voice else None
    yield answer_record(text, speech=synth is not None, cut=cut)
    stats["answer_bytes"] = len(full.encode("utf-8"))
    stats["cut"] = cut
    if synth is not None:
        tts0 = time.monotonic()
        async for rec in speech(full, synth, stats):
            yield rec
        stats["tts_ms"] = int((time.monotonic() - tts0) * 1000)
    yield end_record(END_OK)
