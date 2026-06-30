"""The wall-display web-tool event emitter (jbrain.agent.brainevents). The POST is
best-effort display telemetry — it must never raise into a turn, and it no-ops
cleanly when unconfigured or when there is no running loop."""

import asyncio

import httpx

from jbrain.agent.brainevents import _post_event, build_event_emitter


def test_emit_noop_without_url() -> None:
    # No URL configured -> emit is a silent no-op (and needs no event loop).
    build_event_emitter("")("web_search")  # must not raise


def test_emit_without_running_loop_is_noop() -> None:
    # A URL is set but there is no running loop (sync context) -> skip silently.
    build_event_emitter("http://server-brain:8800/event")("web_search")  # must not raise


async def test_emit_schedules_a_post(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str]] = []

    async def fake_post(url: str, kind: str) -> None:
        calls.append((url, kind))

    monkeypatch.setattr("jbrain.agent.brainevents._post_event", fake_post)
    build_event_emitter("http://server-brain:8800/event")("web_search")
    await asyncio.sleep(0)  # let the fire-and-forget task run
    assert calls == [("http://server-brain:8800/event", "web_search")]


async def test_post_event_swallows_transport_errors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def boom(*_args, **_kwargs) -> None:
        raise httpx.ConnectError("display unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    await _post_event("http://server-brain:8800/event", "web_search")  # must not raise
