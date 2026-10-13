"""The desk panel's turn into the real agents, with the agent and the database faked.

The gate and the thread are proved against Postgres (`test_panel_agent_pg.py`). What is pinned
here is what the panel HEARS: the answer as plain prose short enough to keep, a turn that is
still running left running and named as such, a question or a staged change pointed at the
app rather than pretended done, a busy thread turned away politely — and the PWA's voice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import agent as agent_api
from jbrain.api import endpoint as endpoint_api
from jbrain.api import panel_agent
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.models.note_conversation import AWAITING_OWNER
from tests.unit.test_endpoint_api import (  # noqa: F401
    TestConverse,
    _jerv_frame,
    _provision_panel,
    client,
)

DESK = PrincipalInfo(id="p-desk", kind="device_key", label="panel Jeff", subject_id="s-desk")


# ---- the words ------------------------------------------------------------------------------


def test_markdown_becomes_the_prose_a_panel_can_say() -> None:
    md = (
        "## Today\n\n- **Milk** on the [list](https://x.test/l)\n- Call the *vet*[^1]\n\n"
        "```py\nprint(1)\n```\nSee https://example.test for more."
    )
    assert panel_agent.speakable(md) == "Today. Milk on the list. Call the vet. See for more."


def test_a_short_answer_is_kept_whole() -> None:
    assert panel_agent.condensed("It's 4 pm. Sunny.") == "It's 4 pm. Sunny."


def test_a_long_answer_keeps_whole_sentences_and_says_where_the_rest_is() -> None:
    prose = " ".join(f"Sentence number {i} is here." for i in range(30))
    out = panel_agent.condensed(prose)
    assert len(out.encode()) <= panel_agent.REPLY_BYTES
    assert out.endswith(panel_agent.MORE.strip())
    assert out.startswith("Sentence number 0 is here.")


def test_one_endless_sentence_is_cut_on_a_word() -> None:
    out = panel_agent.condensed("word " * 200)
    assert len(out.encode()) <= panel_agent.REPLY_BYTES
    assert "…" in out and out.endswith(panel_agent.MORE.strip())


def test_a_suffix_survives_the_cut() -> None:
    prose = " ".join(f"Item {i} is long enough to matter." for i in range(20))
    out = panel_agent.condensed(prose, panel_agent.ASKED)
    assert panel_agent.ASKED.strip() in out and len(out.encode()) <= panel_agent.REPLY_BYTES
    assert panel_agent.condensed("Done.", panel_agent.STAGED) == "Done." + panel_agent.STAGED


def test_a_last_sentence_that_fits_alone_but_not_with_the_suffix_is_dropped() -> None:
    prose = "A. " + "b" * 230 + "."
    out = panel_agent.condensed(prose, panel_agent.ASKED)
    assert len(out.encode()) <= panel_agent.REPLY_BYTES and out.startswith("A.")


# ---- the voice ------------------------------------------------------------------------------


class _Store:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw

    async def brain_answer_voice(self, ctx: Any) -> str:
        return self.kw.get("voice", "kokoro-af_heart")

    async def brain_answer_speed(self, ctx: Any) -> float:
        return self.kw.get("speed", 1.0)

    async def brain_answer_pitch(self, ctx: Any) -> float:
        return self.kw.get("pitch", 0.0)

    async def brain_answer_chorus(self, ctx: Any) -> bool:
        return self.kw.get("chorus", False)

    async def brain_answer_robot(self, ctx: Any) -> bool:
        return self.kw.get("robot", False)

    async def brain_read_aloud_engine(self, ctx: Any) -> str:
        return self.kw.get("engine", "piper")

    async def pronunciation_lexicon(self, ctx: Any) -> dict[str, str]:
        if self.kw.get("lexicon_fails"):
            raise RuntimeError("no settings")
        return self.kw.get("lexicon", {})


def _voice(**kw: Any) -> panel_agent.Voice:
    return asyncio.run(panel_agent.owner_voice(cast(Any, _Store(**kw)), SessionContext()))


def test_the_panel_speaks_in_the_pwa_read_aloud_s_voice_and_effects() -> None:
    v = _voice(
        voice="kokoro-bm_george",
        speed=1.2,
        pitch=-2.0,
        chorus=True,
        robot=True,
        lexicon={"Titusville": "Tight us ville"},
    )
    assert v.params == {
        "voice": "kokoro-bm_george",
        "speed": "1.2",
        "pitch": "-2.0",
        "chorus": "1",
        "robot": "1",
    }
    assert panel_agent.tts_params(v, "Off to Titusville.") == {
        "text": "Off to Tight us ville.",
        **v.params,
    }
    assert not v.native


def test_defaults_send_only_the_voice_and_a_native_pwa_is_flagged() -> None:
    v = _voice(engine="native", lexicon_fails=True)
    assert v.params == {"voice": "kokoro-af_heart"} and v.lexicon == {} and v.native
    assert panel_agent.tts_params(None, "hi") == {"text": "hi"}


# ---- the turn -------------------------------------------------------------------------------


class _Live:
    def __init__(self, acc: Any, delay: float = 0.0, boom: bool = False) -> None:
        async def run() -> None:
            await asyncio.sleep(delay)
            if boom:
                raise RuntimeError("turn broke")

        self.task = asyncio.ensure_future(run())
        self.acc = acc


def _acc(text: str = "", stop: str = "end_turn", steps: list[dict[str, Any]] | None = None) -> Any:
    return SimpleNamespace(answer_text=text, stop_reason=stop, tool_steps=lambda: steps or [])


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    seen: dict[str, Any] = {"owner": "owner-1", "threads": [], "chats": []}

    async def owner_id(maker: Any) -> str | None:
        return seen["owner"]

    async def thread(
        maker: Any, sessions: Any, owner: Any, sid: str, agent: str, *, fresh: bool
    ) -> str:
        seen["threads"].append((owner.principal_id, sid, agent, fresh))
        return f"session-{agent}"

    monkeypatch.setattr(panel_agent, "owner_principal_id", owner_id)
    monkeypatch.setattr(panel_agent, "panel_thread", thread)
    monkeypatch.setattr(agent_api, "get_agent_sessions", lambda request: None)
    monkeypatch.setattr(panel_agent, "AGENT_WAIT_S", 0.2)
    yield seen


def _request() -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=None, live_turns={}))
    )


def _chat(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any], make: Any) -> None:
    async def chat(request: Any, principal: PrincipalInfo, body: Any) -> Any:
        seen["chats"].append((principal, body))
        if isinstance(make, HTTPException):
            raise make
        if make is None:
            return SimpleNamespace(headers={})
        request.app.state.live_turns["run-1"] = make()
        return SimpleNamespace(headers={"X-Run-Id": "run-1"})

    monkeypatch.setattr(agent_api, "chat", chat)


def _turn(agent: panel_agent.PanelAgent = "jerv", fresh: bool = False) -> panel_agent.Answer:
    async def go() -> panel_agent.Answer:
        answer, ctx = await panel_agent.run_turn(
            _request(), DESK, agent, "what's next", fresh=fresh
        )
        assert ctx.principal_kind == "owner"
        return answer

    return asyncio.run(go())


def test_the_turn_runs_as_the_owner_in_the_panel_s_thread(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _chat(monkeypatch, wired, lambda: _Live(_acc("**Milk** and eggs.")))
    answer = _turn("dave", fresh=True)
    assert answer.words == "Milk and eggs." and answer.session_id == "session-dave"
    assert wired["threads"] == [("owner-1", "s-desk", "dave", True)]
    principal, body = wired["chats"][0]
    assert (principal.id, principal.kind) == ("owner-1", "owner")
    assert body.session_id == "session-dave"
    assert body.message == "what's next", "only what the owner said is his message"
    assert body.spoken, "the brevity rides the turn's spoken flag, not his words"


def test_the_pwa_strips_the_prefix_old_panel_turns_stored() -> None:
    """Rows from before 0.0.14 hold the prefix in the owner's message; the PWA cuts exactly
    this string, so the two must never drift apart."""
    src = (
        Path(__file__).resolve().parents[3] / "frontend" / "src" / "agent" / "useFullBrain.ts"
    ).read_text(encoding="utf-8")
    assert f'"{panel_agent.LEGACY_VOICE_PREFIX}"' in src


def test_a_question_for_the_owner_is_pointed_at_the_app(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _chat(monkeypatch, wired, lambda: _Live(_acc("Which list?", AWAITING_OWNER)))
    assert _turn().words == "Which list?" + panel_agent.ASKED


def test_a_staged_change_is_waiting_for_his_ok_not_done(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _chat(monkeypatch, wired, lambda: _Live(_acc("", steps=[{"proposal": {"id": "p"}}])))
    assert _turn().words == panel_agent.STAGED.strip()


def test_a_slow_turn_is_left_running_and_named(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    lives: list[_Live] = []

    def make() -> _Live:
        lives.append(_Live(_acc("late"), delay=5))
        return lives[-1]

    _chat(monkeypatch, wired, make)

    async def go() -> panel_agent.Answer:
        answer, _ = await panel_agent.run_turn(_request(), DESK, "jerv", "x", fresh=False)
        assert not lives[0].task.cancelled(), "the panel giving up never cancels the turn"
        lives[0].task.cancel()
        return answer

    answer = asyncio.run(go())
    assert answer.words == panel_agent.STILL_WORKING and answer.stop_reason == "still_running"


def test_a_busy_thread_is_turned_away_politely(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _chat(monkeypatch, wired, HTTPException(status_code=409))
    assert _turn().words == panel_agent.BUSY


def test_any_other_refusal_propagates(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _chat(monkeypatch, wired, HTTPException(status_code=404))
    with pytest.raises(HTTPException):
        _turn()


def test_a_turn_that_vanished_or_broke_says_sorry(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _chat(monkeypatch, wired, None)
    assert _turn().words == panel_agent.FAILED
    _chat(monkeypatch, wired, lambda: _Live(None, boom=True))
    assert _turn().words == panel_agent.FAILED


def test_a_box_with_no_owner_is_503(wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    wired["owner"] = None
    with pytest.raises(HTTPException) as caught:
        _turn()
    assert caught.value.status_code == 503


# ---- the route, with the gate open ------------------------------------------------------------


class TestAgentTurnOnTheRoute:
    URL = "/api/endpoint/converse?persona=jerv"

    def _open(self, monkeypatch: pytest.MonkeyPatch, words: str = "Two things.") -> list[Any]:
        calls: list[Any] = []

        async def allowed(maker: Any, principal: Any) -> bool:
            return True

        async def turn(request: Any, principal: Any, agent: str, heard: str, *, fresh: bool) -> Any:
            calls.append((agent, heard, fresh))
            return panel_agent.Answer(words, "end_turn", "sess", 5), SessionContext()

        async def voice(store: Any, owner: Any) -> panel_agent.Voice:
            return panel_agent.Voice(params={"voice": "kokoro-owner"}, lexicon={})

        monkeypatch.setattr(panel_agent, "agent_allowed", allowed)
        monkeypatch.setattr(panel_agent, "run_turn", turn)
        monkeypatch.setattr(panel_agent, "owner_voice", voice)
        return calls

    def _post(self, c: TestClient, key: str, url: str) -> Any:
        return c.post(url, content=b"\x00\x01" * 1600, headers={"Authorization": f"Bearer {key}"})

    def test_the_frame_carries_the_agent_s_words_in_the_owner_s_voice(
        self,
        client: tuple[TestClient, Path, list[Any]],  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        TestConverse()._wire(c, monkeypatch, heard="what's on today")
        calls = self._open(monkeypatch)
        asked: list[dict[str, str]] = []
        real = panel_agent.tts_params

        def spy(voice: Any, words: str) -> dict[str, str]:
            asked.append(real(voice, words))
            return asked[-1]

        monkeypatch.setattr(panel_agent, "tts_params", spy)
        r = self._post(c, key, self.URL + "&new_thread=true")
        assert r.status_code == 200, r.text
        flags, heard, reply, pcm = _jerv_frame(r.content)
        assert (heard, reply) == ("what's on today", "Two things.")
        assert flags & endpoint_api.JERV_FRAME_AUDIO and pcm
        assert calls == [("jerv", "what's on today", True)]
        assert asked == [{"text": "Two things.", "voice": "kokoro-owner"}]

    def test_dave_answers_and_text_only_never_synthesises(
        self,
        client: tuple[TestClient, Path, list[Any]],  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        TestConverse()._wire(c, monkeypatch)
        calls = self._open(monkeypatch, "Diamonds are deep.")

        def no_tts(**kw: Any) -> Any:
            raise AssertionError("text-only must not synthesise")

        monkeypatch.setattr(endpoint_api.httpx, "AsyncClient", no_tts)
        r = self._post(c, key, "/api/endpoint/converse?persona=dave&voice=false")
        flags, _heard, reply, pcm = _jerv_frame(r.content)
        assert (flags, reply, pcm) == (0, "Diamonds are deep.", b"")
        assert calls[0][0] == "dave"

    def test_a_box_that_cannot_speak_still_sends_the_words(
        self,
        client: tuple[TestClient, Path, list[Any]],  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        TestConverse()._wire(c, monkeypatch)
        self._open(monkeypatch)
        cast(FastAPI, c.app).state.settings.brain_tts_url = ""
        flags, _h, reply, pcm = _jerv_frame(self._post(c, key, self.URL).content)
        assert (flags, reply, pcm) == (0, "Two things.", b"")

    def test_silence_never_reaches_the_agent(
        self,
        client: tuple[TestClient, Path, list[Any]],  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        c, _fw, _sent = client
        key = _provision_panel(c)
        TestConverse()._wire(c, monkeypatch, heard=" ")
        calls = self._open(monkeypatch)
        assert self._post(c, key, self.URL).status_code == 204
        assert calls == []

    def test_with_the_gate_shut_dave_is_refused_and_jerv_is_the_persona(
        self,
        client: tuple[TestClient, Path, list[Any]],  # noqa: F811
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The unit box has no database, so the real gate fails closed — which is the point."""
        c, _fw, _sent = client
        key = _provision_panel(c)
        model = TestConverse()._wire(c, monkeypatch)
        assert self._post(c, key, "/api/endpoint/converse?persona=dave").status_code == 403
        r = self._post(c, key, self.URL)
        assert r.status_code == 200 and model.calls[0]["task"] == "pet.turn"


def test_the_gate_is_closed_to_an_owner_cookie_and_a_keyless_device() -> None:
    owner = PrincipalInfo(id="o", kind="owner", label="owner", subject_id="s")
    bare = PrincipalInfo(id="d", kind="device_key", label="d")
    assert not asyncio.run(panel_agent.agent_allowed(cast(Any, None), owner))
    assert not asyncio.run(panel_agent.agent_allowed(cast(Any, None), bare))
    # A database that cannot answer is a shut gate, not an open one.
    assert not asyncio.run(panel_agent.agent_allowed(cast(Any, None), DESK))


def test_every_sentence_fits_but_the_suffix_does_not() -> None:
    """All of it fits on its own; with the question pointer it no longer does."""
    prose = "x" * 200 + "."
    out = panel_agent.condensed(prose, panel_agent.ASKED)
    assert len(out.encode()) <= panel_agent.REPLY_BYTES and panel_agent.ASKED.strip() in out


# ---- JT2: the whole answer, and what waits for the owner in words ---------------------------


def test_the_screen_keeps_the_whole_answer_or_what_fits_with_an_ellipsis() -> None:
    assert panel_agent.screen_text("Short.", 1000) == ("Short.", False)
    text, cut = panel_agent.screen_text("word " * 400, 1000)
    assert cut and text.endswith("…") and len(text.encode()) <= 1000
    assert not text.endswith(" …")
    text, cut = panel_agent.screen_text("é" * 600, 1000)
    assert cut and len(text.encode()) <= 1000


def test_the_screen_never_splits_an_emoji() -> None:
    """The panel draws emoji code point by code point (firmware-c6 README "Emoji"), so a cut
    must leave each one whole: a stray byte would draw as nothing, or as the wrong glyph."""
    for full in ("\U0001f436" * 300, "Walk the \U0001f436\U0001f415\u200d\U0001f9ba " * 40):
        text, cut = panel_agent.screen_text(full, 255)
        assert cut and len(text.encode()) <= 255
        body = text.removesuffix("…")
        assert full.startswith(body)
        assert body.encode().decode("utf-8") == body


def test_what_waits_is_said_in_words() -> None:
    ask = _acc("", AWAITING_OWNER, [{"name": "ask_owner", "args": {"questions": ["bad"]}}])
    assert panel_agent.waiting(ask, "dave") == "Dave has a question for you. Answer it in the app."
    long_q = "Which " + "very " * 60 + "long thing?"
    asked = panel_agent.waiting(
        _acc("", AWAITING_OWNER, [{"args": {"questions": [{"question": long_q}]}}]), "jerv"
    )
    assert asked.startswith("Jerv asks: Which very") and "… Answer it in the app." in asked
    odd = _acc("", steps=[{"proposal": {"kind": "something-new"}}])
    assert panel_agent.waiting(odd, "jerv") == "Jerv wants to make a change. Confirm it in the app."
    assert panel_agent.waiting(_acc("", steps=[{"proposal": "p"}]), "jerv").startswith(
        "Jerv wants to make a change"
    )
    assert panel_agent.waiting(_acc("Done."), "jerv") == ""


def test_jt1_keeps_its_condensed_words_while_jt2_gets_the_whole_answer(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prose = " ".join(f"Sentence number {i} is here." for i in range(30))
    _chat(monkeypatch, wired, lambda: _Live(_acc(prose)))
    answer = _turn()
    assert len(answer.words.encode()) <= panel_agent.REPLY_BYTES
    assert answer.full == prose
