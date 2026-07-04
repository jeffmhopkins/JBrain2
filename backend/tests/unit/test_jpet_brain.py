"""JPet's talking brain (docs/plans/JPET_PLAN.md W4) — the pet.turn route, faked LLM.

Proves the child's message + pet state become a `pet.turn` call, the structured reply
is parsed, off-enum / null responses degrade safely, and the prompt is a safe kids'
persona.
"""

from datetime import UTC, datetime
from typing import Any

from jbrain.jpet.brain import _system_prompt, pet_turn
from jbrain.jpet.service import Drives, PetStateInfo
from jbrain.llm.types import LlmResult, LlmUsage, parse_json_payload


class _FakeRouter:
    def __init__(self, text: str):
        self._text = text
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        task: str,
        *,
        system: str,
        user_text: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> LlmResult:
        self.calls.append({"task": task, "system": system, "user_text": user_text})
        parsed = parse_json_payload(self._text) if json_schema is not None else None
        return LlmResult(text=self._text, parsed=parsed, usage=LlmUsage(1, 1))


def _state() -> PetStateInfo:
    return PetStateInfo(
        id="p",
        name="Blink",
        domain="general",
        drives=Drives(food=18, energy=80, fun=70, love=74),
        mood="hungry",
        emotion="hungry",
        speech=None,
        asleep=False,
        pos_x=0,
        pos_z=0,
        target_x=0,
        target_z=0,
        facing=0,
        action="idle",
        last_tick_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_pet_turn_parses_structured_reply() -> None:
    router = _FakeRouter('{"speech":"Hee-hee, hallo!","emotion":"happy","action":"play"}')
    reply = await pet_turn(router, state=_state(), message="hi Blink!")
    assert reply.speech == "Hee-hee, hallo!"
    assert reply.emotion == "happy"
    assert reply.action == "play"
    assert router.calls[0]["task"] == "pet.turn"
    assert "hi Blink!" in router.calls[0]["user_text"]
    assert "Blink" in router.calls[0]["system"]


async def test_pet_turn_falls_back_on_unparseable_response() -> None:
    reply = await pet_turn(_FakeRouter("sorry, not json"), state=_state(), message="hi")
    assert reply.speech  # a friendly babble, never empty
    assert reply.emotion == "hungry"  # defaults to the pet's current mood
    assert reply.action == "idle"


async def test_off_enum_and_overlong_are_sanitized() -> None:
    long = "x" * 500
    router = _FakeRouter(f'{{"speech":"{long}","emotion":"furious","action":"fly"}}')
    reply = await pet_turn(router, state=_state(), message="hi")
    assert len(reply.speech) <= 280  # truncated
    assert reply.emotion == "hungry"  # off-enum → the pet's current mood (a valid emotion)
    assert reply.action == "idle"  # off-enum → idle


def test_system_prompt_is_a_safe_kids_persona() -> None:
    prompt = _system_prompt(_state(), ["Emma fed you an apple"])
    low = prompt.lower()
    assert "pet" in low and "safe" in low and "never" in low
    assert "Emma fed you an apple" in prompt  # memories are woven in (W5)
    assert "food=18" in prompt  # drives colour the reply
