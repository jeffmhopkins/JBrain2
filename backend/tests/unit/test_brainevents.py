"""The wall-display web-tool event emitter (jbrain.agent.brainevents). The POST is
best-effort display telemetry — it must never raise into a turn, and it no-ops
cleanly when unconfigured or when there is no running loop."""

import asyncio

import httpx

from jbrain.agent.brainevents import (
    _post_event,
    _post_flag,
    brain_text_enabled,
    build_event_emitter,
    build_flag_emitter,
)


def test_emit_noop_without_url() -> None:
    # No URL configured -> emit is a silent no-op (and needs no event loop).
    build_event_emitter("")("web_search")  # must not raise


def test_emit_without_running_loop_is_noop() -> None:
    # A URL is set but there is no running loop (sync context) -> skip silently.
    build_event_emitter("http://wall:8800/event")("web_search")  # must not raise


async def test_emit_schedules_a_post(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, str | None]] = []

    async def fake_post(url: str, kind: str, text: str | None = None) -> None:
        calls.append((url, kind, text))

    monkeypatch.setattr("jbrain.agent.brainevents._post_event", fake_post)
    build_event_emitter("http://wall:8800/event")("web_search")
    await asyncio.sleep(0)  # let the fire-and-forget task run
    assert calls == [("http://wall:8800/event", "web_search", None)]


async def test_emit_carries_llm_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    posted: list[dict] = []

    async def fake_send(self, url, json):  # type: ignore[no-untyped-def]
        posted.append(json)

        class _R:
            pass

        return _R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_send)
    brain_text_enabled.set(True)  # this turn opted into text
    build_event_emitter("http://wall:8800/event")("llm_input", "what's my next appt?")
    await asyncio.sleep(0)
    assert posted == [{"kind": "llm_input", "text": "what's my next appt?"}]


async def test_emit_drops_text_when_gate_off(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The gate defaults OFF: a query/answer never rides the display unless the turn
    # opted in — only the content-free marker ships.
    posted: list[dict] = []

    async def fake_send(self, url, json):  # type: ignore[no-untyped-def]
        posted.append(json)

        class _R:
            pass

        return _R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_send)
    brain_text_enabled.set(False)
    build_event_emitter("http://wall:8800/event")("web_search", "a personal query")
    await asyncio.sleep(0)
    assert posted == [{"kind": "web_search"}]  # marker only, text dropped


async def test_post_event_truncates_long_text(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    posted: list[dict] = []

    async def fake_send(self, url, json):  # type: ignore[no-untyped-def]
        posted.append(json)

        class _R:
            pass

        return _R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_send)
    await _post_event("http://wall:8800/event", "llm_output", "x" * 9000)
    # Bounded so a pathologically long answer can't bloat the POST / the display buffer —
    # generous now (the wall reads the whole reply aloud), not a glance excerpt.
    assert len(posted[0]["text"]) == 4000


async def test_post_event_swallows_transport_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def boom(*_args, **_kwargs) -> None:
        raise httpx.ConnectError("display unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    await _post_event("http://wall:8800/event", "web_search")  # must not raise


def test_flag_emit_noop_without_url() -> None:
    # No URL configured -> the flag emitter is a silent no-op (and needs no loop).
    build_flag_emitter("")("read_aloud", True)  # must not raise


def test_flag_emit_without_running_loop_is_noop() -> None:
    build_flag_emitter("http://wall:8800/event")("read_aloud", True)  # must not raise


async def test_flag_emit_ships_regardless_of_text_gate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The read-aloud flag is display config, not owner text: it ships even with the
    # per-turn text gate OFF (unlike the text emitter).
    posted: list[dict] = []

    async def fake_send(self, url, json):  # type: ignore[no-untyped-def]
        posted.append(json)

        class _R:
            pass

        return _R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_send)
    brain_text_enabled.set(False)
    build_flag_emitter("http://wall:8800/event")("read_aloud", True)
    await asyncio.sleep(0)
    assert posted == [{"kind": "read_aloud", "on": True}]


async def test_post_flag_swallows_transport_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def boom(*_args, **_kwargs) -> None:
        raise httpx.ConnectError("display unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    await _post_flag("http://wall:8800/event", "read_aloud", True)  # must not raise
