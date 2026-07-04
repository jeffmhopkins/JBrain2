"""JPet's talking brain (docs/proposed/JPET_V2_PLAN.md) — the pet.turn route, faked LLM.

Proves the child's message + pet state become a `pet.turn` call, the structured reply
(now `{speech, emotion, script}`) is parsed and the script is bounded, off-enum / null
responses degrade safely, and the prompt is a safe kids' persona listing the primitives
and in-scene objects.
"""

from datetime import UTC, datetime
from typing import Any

from jbrain.jpet.brain import _system_prompt, pet_turn
from jbrain.jpet.service import OBJECT_HOMES, PetStateInfo
from jbrain.llm.types import LlmResult, LlmUsage, parse_json_payload

OBJS = {k: (v[0], v[1]) for k, v in OBJECT_HOMES.items()}


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
        mood="happy",
        emotion="happy",
        speech=None,
        asleep=False,
        pos_x=0,
        pos_z=0,
        target_x=0,
        target_z=0,
        facing=0,
        action="idle",
        objects=OBJS,
        last_tick_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_pet_turn_parses_structured_reply_with_a_script() -> None:
    router = _FakeRouter(
        '{"speech":"Watch me!","emotion":"excited",'
        '"script":[{"action":"spin","duration_ms":1200},{"action":"dance"}]}'
    )
    reply = await pet_turn(router, state=_state(), message="dance for me!", objects=OBJS)
    assert reply.speech == "Watch me!"
    assert reply.emotion == "excited"
    assert [s.action for s in reply.script] == ["spin", "dance", "sit"]  # terminating step added
    assert router.calls[0]["task"] == "pet.turn"
    assert "dance for me!" in router.calls[0]["user_text"]


async def test_pet_turn_drops_ungrounded_script_steps() -> None:
    router = _FakeRouter(
        '{"speech":"ok","emotion":"happy","script":[{"action":"go_to","target":"moon"},{"action":"jump"}]}'
    )
    reply = await pet_turn(router, state=_state(), message="go to the moon", objects=OBJS)
    assert [s.action for s in reply.script] == ["jump", "sit"]  # moon dropped, terminates


async def test_pet_turn_falls_back_on_unparseable_response() -> None:
    reply = await pet_turn(
        _FakeRouter("sorry, not json"), state=_state(), message="hi", objects=OBJS
    )
    assert reply.speech  # a friendly babble, never empty
    assert reply.emotion == "happy"  # defaults to the pet's current mood
    assert reply.script == []  # nothing to act out


async def test_off_enum_and_overlong_are_sanitized() -> None:
    long = "x" * 500
    router = _FakeRouter(f'{{"speech":"{long}","emotion":"furious","script":[]}}')
    reply = await pet_turn(router, state=_state(), message="hi", objects=OBJS)
    assert len(reply.speech) <= 280  # truncated
    assert reply.emotion == "happy"  # off-enum → the pet's current mood (a valid emotion)


def test_system_prompt_is_a_safe_kids_persona() -> None:
    prompt = _system_prompt(_state(), ["Emma fed you an apple"], OBJS)
    low = prompt.lower()
    assert "pet" in low and "safe" in low and "never" in low
    assert "Emma fed you an apple" in prompt  # memories are woven in
    assert "ball" in low and "dance" in low  # room objects + primitives are listed
