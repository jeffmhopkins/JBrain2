"""Every model call narrated for the vitals page (jbrain.model_calls).

The contract the page reads against: a call is recorded when it STARTS, settles with how it
ended (an abandoned one as `stopped`, not left open to age out), carries who asked, and none
of it is ever awaited by the call or able to break it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jbrain import model_calls
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.llm.types import UserMessage

Writes = list[tuple[str, dict[str, Any]]]


@pytest.fixture(autouse=True)
def _unwired() -> Any:
    model_calls.reset()
    yield
    model_calls.reset()


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> Writes:
    """What would be written, with the writer wired to a maker it never touches."""
    captured: Writes = []

    async def fake_write(sql: str, params: dict[str, Any]) -> None:
        captured.append((sql, params))

    monkeypatch.setattr(model_calls, "_write", fake_write)
    model_calls.configure(object(), source="worker")  # type: ignore[arg-type]
    return captured


async def _drain() -> None:
    await asyncio.gather(*model_calls._TASKS)


async def test_a_call_is_opened_when_it_starts_and_settled_when_it_ends(writes: Writes) -> None:
    async with model_calls.track(
        model_calls.CREATURE, "think", model="qwen3-vl", persona_id="p1", robot="gb-a"
    ):
        await asyncio.sleep(0)
        assert len(writes) == 1
        opened = writes[0][1]
        assert (opened["caller"], opened["label"], opened["model"]) == (
            "creature",
            "think",
            "qwen3-vl",
        )
        assert (opened["persona_id"], opened["robot"], opened["source"]) == ("p1", "gb-a", "worker")
    await _drain()
    sql, settled = writes[-1]
    assert "UPDATE" in sql
    assert settled == {"id": opened["id"], "status": "ok", "error": ""}


async def test_the_call_never_waits_on_its_own_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database that hangs costs the row, never the reply."""
    gate = asyncio.Event()

    async def stuck_write(sql: str, params: dict[str, Any]) -> None:
        await gate.wait()

    monkeypatch.setattr(model_calls, "_write", stuck_write)
    model_calls.configure(object(), source="api")  # type: ignore[arg-type]
    async with asyncio.timeout(1):
        async with model_calls.track(model_calls.ASSISTANT, "assistant turn", model="m"):
            pass
    gate.set()
    await _drain()


async def test_a_raised_call_settles_failed_and_still_raises(writes: Writes) -> None:
    with pytest.raises(RuntimeError):
        async with model_calls.track(model_calls.BACKGROUND, "x", model="m"):
            raise RuntimeError("gateway gone")
    await _drain()
    assert writes[-1][1]["status"] == "failed"
    assert "gateway gone" in writes[-1][1]["error"]


async def test_an_in_band_failure_settles_failed(writes: Writes) -> None:
    async with model_calls.track(model_calls.CODE, "coding", model="m") as call:
        call.failed("gateway HTTP 500")
    await _drain()
    assert writes[-1][1] == {
        "id": writes[0][1]["id"],
        "status": "failed",
        "error": "gateway HTTP 500",
    }


async def test_an_abandoned_call_settles_stopped(writes: Writes) -> None:
    """An owner's Stop or a phone hanging up cancels the call; its row must not sit open."""
    started = asyncio.Event()

    async def call() -> None:
        async with model_calls.track(model_calls.CREATURE, "think", model="m"):
            started.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(call())
    await started.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await _drain()
    assert writes[-1][1]["status"] == "stopped"


async def test_unwired_records_nothing_and_still_runs_the_call() -> None:
    async with model_calls.track(model_calls.ASSISTANT, "x", model="m") as call:
        call.failed("ignored")
    assert not model_calls._TASKS


def test_router_tasks_are_listed_in_the_owners_words() -> None:
    assert model_calls.router_caller("agent.turn") == ("assistant", "assistant turn")
    assert model_calls.router_caller("pet.thought") == ("pet", "pet daydreaming")
    assert model_calls.router_caller("vision.ocr") == ("background", "reading text in an image")
    # A task added later still lists, by its own name.
    assert model_calls.router_caller("new.thing_x") == ("background", "new thing x")


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def fake_track(caller: str, label: str, **kw: Any) -> AsyncIterator[Any]:
        seen.append({"caller": caller, "label": label, **kw})
        yield model_calls.Call({"status": "ok", "error": ""})

    monkeypatch.setattr(model_calls, "track", fake_track)
    return seen


async def test_every_router_entry_point_is_tracked(tracked: list[dict[str, Any]]) -> None:
    fake = FakeLlmClient(["one", "two", "three"])
    router = LlmRouter(
        {"xai": fake}, {"video.summarize": ("xai", "grok-4.3"), "agent.turn": ("xai", "grok-4.3")}
    )
    await router.complete("video.summarize", system="s", user_text="u")
    await router.converse("agent.turn", system="s", messages=[UserMessage(text="hi")])
    async for _ in router.converse_stream(
        "agent.turn", system="s", messages=[UserMessage(text="hi")]
    ):
        pass
    assert [(t["caller"], t["task"], t["model"], t["local"]) for t in tracked] == [
        ("background", "video.summarize", "grok-4.3", False),
        ("assistant", "agent.turn", "grok-4.3", False),
        ("assistant", "agent.turn", "grok-4.3", False),
    ]


def _creature_turn(tmp_path: Path, gateway_status: int) -> None:
    from tests.unit.test_growbot_llm import _COMPLETIONS, _app, _chat, _Gateway, _Residency

    app, auth, _ = _app(tmp_path, residency=_Residency(), gateway=_Gateway(status=gateway_status))
    headers = {**auth, "X-Growbot-Persona": _PERSONA, "X-Growbot-Robot": "gb-dave1"}
    assert TestClient(app).post(_COMPLETIONS, json=_chat(), headers=headers).status_code == 200


_PERSONA = "0b3c7f1e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"


def test_a_creature_think_is_tracked_with_its_persona(
    tmp_path: Path, tracked: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reading that started this: two Daves thinking, and nothing on the page said so."""
    outcomes: list[dict[str, str]] = []
    real_call = model_calls.Call

    def spy(outcome: dict[str, str]) -> model_calls.Call:
        outcomes.append(outcome)
        return real_call(outcome)

    monkeypatch.setattr(model_calls, "Call", spy)
    _creature_turn(tmp_path, 200)
    (t,) = tracked
    assert (t["caller"], t["label"], t["persona_id"], t["robot"]) == (
        "creature",
        "think",
        _PERSONA,
        "gb-dave1",
    )
    assert outcomes[0]["status"] == "ok"


def test_a_creature_turn_the_gateway_refused_is_tracked_failed(
    tmp_path: Path, tracked: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[dict[str, str]] = []
    real_call = model_calls.Call

    def spy(outcome: dict[str, str]) -> model_calls.Call:
        outcomes.append(outcome)
        return real_call(outcome)

    monkeypatch.setattr(model_calls, "Call", spy)
    _creature_turn(tmp_path, 500)
    assert outcomes[0] == {"status": "failed", "error": "gateway HTTP 500"}
