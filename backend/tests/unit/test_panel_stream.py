"""JT2: the desk panel's turn as a stream (firmware-c6 0.0.12), with the agent, Whisper and
Kokoro faked.

What is pinned: the bytes (so the firmware's parser has a fixed target), the status lines a
tool run turns into, keep-alives through every silence, the whole answer and its speech
sentence by sentence, the cap, and — the property that matters most — a panel going away
stops the synthesis and never touches the agent's turn.
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jbrain.api import panel_agent, panel_stream
from jbrain.api.panel_labels import ING, STEP_LABELS
from jbrain.models.note_conversation import AWAITING_OWNER

_SUMMARY_TS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "agent" / "toolSummary.ts"


def parse(body: bytes) -> list[tuple[int, bytes]]:
    """The reader the firmware implements, in Python: magic, then type/u16/payload records."""
    assert body[:3] == b"JT2"
    out, i = [], 3
    while i < len(body):
        kind, n = struct.unpack_from("<BH", body, i)
        assert n <= panel_stream.MAX_RECORD
        out.append((kind, body[i + 3 : i + 3 + n]))
        i += 3 + n
    assert i == len(body), "no record is cut short"
    return out


async def collect(gen: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in gen])


# ---- the bytes ---------------------------------------------------------------------------------


def test_a_record_is_type_length_payload() -> None:
    assert panel_stream.record(panel_stream.ALIVE) == b"\x03\x00\x00"
    assert panel_stream.text_record(panel_stream.HEARD, "hé") == b"\x01\x03\x00h\xc3\xa9"
    with pytest.raises(ValueError):
        panel_stream.record(panel_stream.PCM, b"\x00" * (panel_stream.MAX_RECORD + 1))


def test_a_status_is_one_line_cut_on_a_character() -> None:
    rec = panel_stream.status_record("Searching the web: " + "é" * 60)
    assert rec[0] == panel_stream.STATUS
    text = rec[3:].decode("utf-8")
    assert len(rec[3:]) <= panel_stream.STATUS_BYTES and text.endswith("…")


def test_speech_goes_in_even_records_no_longer_than_the_panel_takes() -> None:
    recs = panel_stream.pcm_records(b"\x01" * 9001)
    sizes = [struct.unpack_from("<H", r, 1)[0] for r in recs]
    assert sizes == [4096, 4096, 808] and all(r[0] == panel_stream.PCM for r in recs)


def test_a_finished_turn_is_the_same_records_a_live_one_ends_with() -> None:
    recs = parse(panel_stream.whole("hi", "Hello there.", b"\x00\x00" * 10))
    assert [k for k, _ in recs] == [
        panel_stream.HEARD,
        panel_stream.ANSWER,
        panel_stream.PCM,
        panel_stream.END,
    ]
    assert recs[1][1] == bytes([panel_stream.ANSWER_SPEECH]) + b"Hello there."
    assert recs[3][1] == bytes([panel_stream.END_OK])
    silent = parse(panel_stream.whole("hi", "x" * 1500, b""))
    assert silent[1][1][0] == panel_stream.ANSWER_CUT, "cut, and no speech follows"
    assert len(silent[1][1]) - 1 <= panel_stream.TEXT_BYTES


# ---- what the agent is doing -------------------------------------------------------------------


def test_the_labels_are_the_pwa_s_own() -> None:
    """`panel_labels.STEP_LABELS` is the PWA's map verbatim, and every verb has a present."""
    src = _SUMMARY_TS.read_text(encoding="utf-8")
    start = src.index("const STEP_LABELS: Record<string, string> = {")
    body = src[start : src.index("\n};", start)]
    pwa = dict(re.findall(r'^\s*(\w+): "([^"]+)"', body, re.MULTILINE))
    assert pwa == STEP_LABELS, "toolSummary.ts STEP_LABELS changed: copy it to panel_labels.py"
    missing = {label.split()[0] for label in pwa.values()} - set(ING)
    assert not missing, f"give these verbs a present tense in panel_labels.ING: {missing}"


def test_a_tool_reads_as_what_it_is_doing() -> None:
    label = panel_stream.progress_label
    assert label("web_search", {"query": "high tide Port St John FL"}) == (
        "Searching the web: high tide Port St John FL…"
    )
    assert label("weather") == "Checking the weather…"
    assert label("web_fetch", {"url": "https://www.tides.example/x/y"}) == (
        "Reading a web page: tides.example…"
    )
    assert label("lookup_tide_tables") == "Checking tide tables…"
    assert label("brand_new_tool") == "Working…"
    for name in STEP_LABELS:
        assert label(name).endswith("…") and not label(name)[0].islower()


def _frame(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def test_progress_says_each_change_once() -> None:
    p = panel_stream.Progress()
    seen = [
        p.feed(_frame({"type": "reasoning_delta", "text": "hm"})),
        p.feed(_frame({"type": "tool_call", "id": "1", "name": "web_search", "arguments": {}})),
        p.feed(_frame({"type": "tool_result", "tool_call_id": "1"})),
        p.feed(_frame({"type": "reasoning_delta", "text": "so"})),
        p.feed(_frame({"type": "reasoning_delta", "text": "and"})),
        p.feed(_frame({"type": "tool_progress", "tool_call_id": "2", "label": "Frame 3/9"})),
        p.feed(_frame({"type": "text_delta", "text": "High"})),
        p.feed(_frame({"type": "text_delta", "text": " tide"})),
        p.feed(b": keepalive\n\n"),
        p.feed(b"data: not json\n\n"),
        p.feed(b"data: [1]\n\n"),
    ]
    assert seen == [
        None,
        "Searching the web…",
        None,
        "Thinking…",
        None,
        "Frame 3/9…",
        "Writing the answer…",
        None,
        None,
        None,
        None,
    ]
    assert p.tools == 1


# ---- the speech ---------------------------------------------------------------------------------


def test_the_first_group_is_one_sentence_then_longer_groups() -> None:
    text = "First one. " + " ".join(f"Sentence {i} is here." for i in range(60))
    groups = panel_stream.speech_groups(text)
    assert groups[0] == "First one."
    assert all(len(g) <= panel_stream.GROUP_CHARS for g in groups[1:])
    assert " ".join(groups) == text.strip()
    huge = panel_stream.speech_groups("word " * 400)
    assert all(len(g) <= panel_stream.SENTENCE_CHARS for g in huge)
    assert panel_stream.speech_groups("   ") == []


def _pcm(n: int) -> bytes:
    return b"\x01\x00" * n


def test_speech_is_every_group_in_order_one_synthesised_ahead() -> None:
    order: list[str] = []

    async def synth(words: str, budget: int) -> bytes:
        order.append(words)
        return _pcm(3000)

    async def go() -> tuple[bytes, dict[str, Any]]:
        stats: dict[str, Any] = {}
        body = await collect(panel_stream.speech("One. Two. Three.", synth, stats))
        return body, stats

    body, stats = asyncio.run(go())
    assert order == ["One.", "Two. Three."]
    assert stats["speech_bytes"] == 12000 and stats["groups"] == 2
    assert len(body) == 12000 + 4 * 3  # each 6,000-byte group in two records of ≤ 4,096


def test_speech_stops_at_the_cap_and_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel_stream, "SPEECH_CAP_S", 0.1)  # 3,200 bytes

    async def synth(words: str, budget: int) -> bytes:
        return _pcm(2000)

    async def broken(words: str, budget: int) -> bytes:
        raise RuntimeError("kokoro down")

    async def go() -> tuple[dict[str, Any], dict[str, Any]]:
        capped: dict[str, Any] = {}
        await collect(panel_stream.speech("A. B. C. D.", synth, capped))
        failed: dict[str, Any] = {}
        assert await collect(panel_stream.speech("A. B.", broken, failed)) == b""
        return capped, failed

    capped, failed = asyncio.run(go())
    assert capped["speech_bytes"] == 3200 and capped["speech_capped"]
    assert failed["speech_failed"]


def test_a_slow_group_is_covered_by_keep_alives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel_stream, "ALIVE_S", 0.01)

    async def synth(words: str, budget: int) -> bytes:
        await asyncio.sleep(0.05)
        return _pcm(10)

    body = asyncio.run(collect(panel_stream.speech("Slow.", synth, {})))
    assert body.startswith(panel_stream.record(panel_stream.ALIVE))


def test_a_panel_going_away_stops_the_synthesis() -> None:
    cancelled: list[str] = []

    async def synth(words: str, budget: int) -> bytes:
        try:
            if words != "One.":
                await asyncio.sleep(10)
            return _pcm(100)
        except asyncio.CancelledError:
            cancelled.append(words)
            raise

    async def go() -> None:
        gen = panel_stream.speech("One. Two.", synth, {})
        await gen.__anext__()  # the first group's record, the second already synthesising
        await asyncio.sleep(0.01)
        await gen.aclose()  # the panel went away

    asyncio.run(go())
    assert cancelled == ["Two."]


# ---- the turn -----------------------------------------------------------------------------------


class _Live:
    """The PWA turn's live broker, as far as the stream uses it: `stream(after)` and `task`."""

    def __init__(self, frames: list[bytes], acc: Any, gap: float = 0.0, hang: bool = False) -> None:
        self.frames, self.acc, self.gap, self.hang = frames, acc, gap, hang
        self._task: asyncio.Task[None] | None = None

    @property
    def task(self) -> asyncio.Task[None]:
        if self._task is None:

            async def run() -> None:
                await asyncio.sleep(3600 if self.hang else self.gap * (len(self.frames) + 1))

            self._task = asyncio.ensure_future(run())
        return self._task

    async def stream(self, after: int = 0) -> AsyncIterator[bytes]:
        for frame in self.frames:
            await asyncio.sleep(self.gap)
            yield frame
        if self.hang:
            await asyncio.sleep(3600)
        await self.task


def _acc(text: str, stop: str = "end_turn", steps: list[dict[str, Any]] | None = None) -> Any:
    return SimpleNamespace(answer_text=text, stop_reason=stop, tool_steps=lambda: steps or [])


def _turn(
    live: Any = None,
    *,
    heard: str = "high tide at Port St John",
    voice: bool = True,
    early: panel_agent.Answer | None = None,
    stt_fails: bool = False,
    synth_log: list[str] | None = None,
    cap_s: float = 5.0,
) -> panel_stream.Turn:
    async def transcribe() -> str:
        if stt_fails:
            raise RuntimeError("whisper down")
        return heard

    async def start(words: str) -> panel_agent.Started:
        s = panel_agent.Started(
            panel_agent.SessionContext(), "session-1", asyncio.get_running_loop().time()
        )
        s.live = live
        if early is not None:
            s.early = early
        return s

    async def synth(started: panel_agent.Started) -> panel_stream.Synth:
        async def say(words: str, budget: int) -> bytes:
            if synth_log is not None:
                synth_log.append(words)
            return _pcm(100)

        return say

    return panel_stream.Turn(
        transcribe=transcribe, start=start, synth=synth, agent="jerv", voice=voice, cap_s=cap_s
    )


def _run(turn: panel_stream.Turn) -> list[tuple[int, bytes]]:
    return parse(asyncio.run(collect(panel_stream.agent_turn(turn))))


def test_a_tool_run_streams_status_then_the_whole_answer_and_its_speech() -> None:
    frames = [
        _frame(
            {"type": "tool_call", "id": "1", "name": "web_search", "arguments": {"query": "tides"}}
        ),
        _frame({"type": "tool_call", "id": "2", "name": "web_fetch", "arguments": {}}),
        _frame({"type": "text_delta", "text": "High"}),
    ]
    answer = "**High tide** is at 4:12 pm. Low tide is at 10:30 pm. " + "More detail. " * 120
    said: list[str] = []
    recs = _run(_turn(_Live(frames, _acc(answer)), synth_log=said))
    kinds = [k for k, _ in recs]
    assert kinds[:5] == [
        panel_stream.HEARD,
        panel_stream.STATUS,
        panel_stream.STATUS,
        panel_stream.STATUS,
        panel_stream.STATUS,
    ]
    assert recs[0][1] == b"high tide at Port St John"
    assert [p.decode() for k, p in recs if k == panel_stream.STATUS] == [
        "Thinking…",
        "Searching the web: tides…",
        "Reading a web page…",
        "Writing the answer…",
    ]
    (flags, text) = next((p[0], p[1:]) for k, p in recs if k == panel_stream.ANSWER)
    assert flags == panel_stream.ANSWER_SPEECH | panel_stream.ANSWER_CUT
    assert text.decode().startswith("High tide is at 4:12 pm.") and text.decode().endswith("…")
    assert len(text) <= panel_stream.TEXT_BYTES
    # The speech is the WHOLE answer, past what the screen keeps.
    assert said[0] == "High tide is at 4:12 pm." and " ".join(said).count("More detail.") == 120
    assert kinds[-1] == panel_stream.END and recs[-1][1] == bytes([panel_stream.END_OK])
    assert panel_stream.PCM in kinds


def test_voice_off_sends_the_words_and_no_speech() -> None:
    recs = _run(_turn(_Live([], _acc("Done.")), voice=False))
    answer = next(p for k, p in recs if k == panel_stream.ANSWER)
    assert answer == b"\x00Done."
    assert panel_stream.PCM not in [k for k, _ in recs]


def test_nothing_heard_and_a_deaf_box_end_plainly() -> None:
    assert _run(_turn(heard="")) == [(panel_stream.END, bytes([panel_stream.END_NOTHING]))]
    end = _run(_turn(stt_fails=True))[-1]
    assert end[0] == panel_stream.END and end[1][0] == panel_stream.END_FAILED


def test_a_busy_thread_answers_at_once_without_pointing_at_the_app() -> None:
    busy = panel_agent.Answer(panel_agent.BUSY, "busy", "s", 0, panel_agent.BUSY_STREAM)
    recs = _run(_turn(early=busy, voice=False))
    assert next(p for k, p in recs if k == panel_stream.ANSWER)[1:] == (
        panel_agent.BUSY_STREAM.encode()
    )
    assert "app" not in panel_agent.BUSY_STREAM


def test_a_question_says_what_it_is() -> None:
    steps = [{"name": "ask_owner", "args": {"questions": [{"question": "Which beach?"}]}}]
    recs = _run(_turn(_Live([], _acc("I need to check.", AWAITING_OWNER, steps)), voice=False))
    text = next(p for k, p in recs if k == panel_stream.ANSWER)[1:].decode()
    assert text == "I need to check. Jerv asks: Which beach? Answer it in the app."


def test_a_staged_change_says_what_waits() -> None:
    steps = [{"name": "manage_appointment", "proposal": {"kind": "appointment"}}]
    recs = _run(_turn(_Live([], _acc("", steps=steps)), voice=False))
    text = next(p for k, p in recs if k == panel_stream.ANSWER)[1:].decode()
    assert text == "Jerv wants to change your calendar. Confirm it in the app."


def test_quiet_stretches_are_kept_alive_and_the_cap_leaves_the_turn_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(panel_stream, "ALIVE_S", 0.01)

    async def go() -> tuple[list[tuple[int, bytes]], _Live]:
        live = _Live([], _acc("late"), hang=True)
        body = await collect(panel_stream.agent_turn(_turn(live, voice=False, cap_s=0.08)))
        assert not live.task.done(), "the panel's cap never ends the owner's turn"
        live.task.cancel()
        return parse(body), live

    recs, _ = asyncio.run(go())
    kinds = [k for k, _ in recs]
    assert kinds.count(panel_stream.ALIVE) >= 3
    text = next(p for k, p in recs if k == panel_stream.ANSWER)[1:].decode()
    assert text == panel_agent.STILL_WORKING_STREAM


def test_a_panel_going_away_mid_turn_leaves_the_turn_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(panel_stream, "ALIVE_S", 0.01)

    async def go() -> bool:
        live = _Live(
            [_frame({"type": "tool_call", "id": "1", "name": "weather"})], _acc("x"), hang=True
        )
        gen = panel_stream.agent_turn(_turn(live))
        seen = b""
        while b"Checking the weather" not in seen:
            seen += await gen.__anext__()
        await gen.aclose()
        alive = not live.task.done()
        live.task.cancel()
        return alive

    assert asyncio.run(go()), "BOOT on the panel never cancels the owner's turn"


def test_a_turn_that_never_starts_is_an_answer_too() -> None:
    async def start(words: str) -> panel_agent.Started:
        raise RuntimeError("no owner")

    turn = _turn()
    turn.start = start
    recs = _run(turn)
    assert recs[-1][0] == panel_stream.END and recs[-1][1][0] == panel_stream.END_FAILED
    assert next(p for k, p in recs if k == panel_stream.ANSWER)[1:] == panel_agent.FAILED.encode()
