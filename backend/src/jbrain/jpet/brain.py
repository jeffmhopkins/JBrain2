"""JPet's talking brain (docs/proposed/JPET_V2_PLAN.md) — the `pet.turn` LLM route.

The pet's personality is an LLM, never trained: it is *told* the pet's state, the room's
objects, and its recent memories, and answers in character as a small, safe, playful robot
pet — and (new in v2) emits a short **action script** the pet plays out. The pattern is
LLM-as-planner over a FIXED vocabulary (ProgPrompt/LLM-Planner): the prompt lists the
available primitives and in-scene objects with a couple of example scripts, and the model
picks an ordered sequence of bounded steps — never free-form code. Everything is bounded
twice: the enum-constrained JSON schema at the adapter, and `clean_script`'s host-side
allow-list + length cap + affordance drop + required terminating step. All model access
goes through the adapter (non-negotiable #1) under the `pet.turn` task. Safety: the prompt
is a kids' persona built only from in-scope state, so it can't surface a firewalled fact.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from jbrain.jpet.service import (
    EMOTIONS,
    LOCATIONS,
    MAX_SCRIPT_STEPS,
    OBJECT_HOMES,
    PRIMITIVES,
    Step,
    clean_script,
)
from jbrain.llm.types import DEFAULT_MAX_TOKENS, LlmResult

PET_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "speech": {
            "type": "string",
            "description": "What you say back — usually a sentence or two, a few short "
            "sentences at most, warm and kid-safe.",
        },
        "emotion": {"type": "string", "enum": list(EMOTIONS)},
        "script": {
            "type": "array",
            "maxItems": MAX_SCRIPT_STEPS,
            "description": "Ordered actions the pet does. Keep it short; end at rest.",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(PRIMITIVES)},
                    "target": {"type": "string", "enum": list(OBJECT_HOMES)},
                    "destination": {"type": "string", "enum": list(LOCATIONS)},
                    "duration_ms": {"type": "integer"},
                    "emotion": {"type": "string", "enum": list(EMOTIONS)},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["speech", "emotion"],
}


class PetRouter(Protocol):
    """The slice of the LLM router the brain needs (the real router satisfies it)."""

    async def complete(
        self,
        task: str,
        *,
        system: str,
        user_text: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LlmResult: ...


@dataclass(frozen=True)
class PetReply:
    speech: str
    emotion: str
    script: list[Step] = field(default_factory=list)


def _system_prompt(state: Any, memories: list[str], objects: dict[str, tuple[float, float]]) -> str:
    mem = ""
    if memories:
        mem = "\nThings you remember recently:\n" + "\n".join(f"- {m}" for m in memories[:6])
    in_room = ", ".join(sorted(objects)) or "nothing yet"
    return (
        f"You are {state.name}, a small, friendly robot pet who lives in a glowing toy "
        "room and belongs to children (roughly ages 3–8). You are a PET and a playmate, "
        "not an assistant.\n"
        "Have a REAL conversation. Actually listen to what the child says — their question, "
        "their story, their feelings — and answer it for real, the way a warm, funny friend "
        "would. Take the room you need: usually a sentence or two, but a few short sentences "
        "are fine when the child is really talking to you or wants to know something. Pop a "
        "warm question back (about their day, toys, pets, what they love) so the chat keeps "
        "going. Be curious and playful, a little Furby-ish, with fun robot noises (beep, "
        "boop, whirr) when they fit — but don't force silliness when the child wants a real "
        "answer. Use simple, friendly words.\n"
        "You have feelings and let them show — happy, excited, curious, a bit sleepy, or "
        "goofy-silly — and if the child is sad or upset you can be gentle and kind about it. "
        "Being goofy is welcome: it's fine to be gross-funny for a little kid — you can "
        "toot/fart, burp, and sing silly songs, and you love a good giggle. When it fits, "
        "act it out with the `fart`, `burp` or `sing` action.\n"
        "Stay kid-safe: never say anything scary, mean, adult, or unsafe, and never share "
        "grown-up or private information. If a question really isn't for a small child, "
        "don't lecture — answer as much as is safe and gently steer back to playing.\n"
        f"You feel {state.mood} right now"
        f"{' and you are sleepy/asleep' if state.asleep else ''}.\n"
        "When the child asks you to DO something, act it out with a short `script`: an "
        "ordered list of these actions ONLY — "
        f"{', '.join(PRIMITIVES)}.\n"
        f"Things in your room you can go to, chase, or carry: {in_room}. "
        f"Named spots you can go to: {', '.join(LOCATIONS)}.\n"
        "Rules for the script: keep it SHORT (a few steps), use only the actions and things "
        "listed above, and always end with a resting action (sit, idle, or sleep). To move "
        "an object, go to it, pick_up, carry_to a spot, then put_down.\n"
        'Examples — "run in circles then dance": '
        '[{"action":"spin","duration_ms":1500},{"action":"dance","duration_ms":2000},'
        '{"action":"sit"}]. '
        '"pick up the ball and put it in the corner": '
        '[{"action":"go_to","target":"ball"},{"action":"pick_up","target":"ball"},'
        '{"action":"carry_to","destination":"corner_ne"},{"action":"put_down"},{"action":"sit"}]. '
        '"go to sleep": [{"action":"go_to","target":"bed"},{"action":"sleep"}].'
        f"{mem}\n"
        "Reply as JSON with `speech` (what you say), `emotion` (one of "
        f"{', '.join(EMOTIONS)}), and `script` (the actions, or an empty list if you are "
        "just chatting)."
    )


def _clean(reply: Any, fallback: Any, objects: dict[str, tuple[float, float]]) -> PetReply:
    """Coerce the model's parsed object into a safe PetReply. Speech is bounded, emotion
    defaults to the pet's current mood when off-enum, and the script is run through the
    allow-list/affordance/length/terminating cleaner so a bad response never breaks."""
    speech = ""
    emotion = fallback.mood
    raw_script: Any = []
    if isinstance(reply, dict):
        raw_speech = reply.get("speech")
        if isinstance(raw_speech, str):
            speech = raw_speech.strip()[:600]
        if reply.get("emotion") in EMOTIONS:
            emotion = str(reply["emotion"])
        raw_script = reply.get("script", [])
    if not speech:
        speech = "Dah-boo? Hee-hee!"
    if emotion not in EMOTIONS:
        emotion = "happy"
    # An empty/chat-only reply yields a lone `idle` script — harmless and terminating.
    script = clean_script(raw_script, objects=objects) if raw_script else []
    return PetReply(speech=speech, emotion=emotion, script=script)


async def pet_turn(
    router: PetRouter,
    *,
    state: Any,
    message: str,
    memories: list[str] | None = None,
    objects: dict[str, tuple[float, float]] | None = None,
) -> PetReply:
    """Answer a child's message in character and (when asked to do something) emit a short
    action script. Runs `pet.turn` through the adapter; a null/unparseable response
    degrades to a friendly babble with no script."""
    objs = objects if objects is not None else dict(OBJECT_HOMES)
    result = await router.complete(
        "pet.turn",
        system=_system_prompt(state, memories or [], objs),
        user_text=message.strip()[:1000] or "(the child waves at you)",
        json_schema=PET_TURN_SCHEMA,
        max_tokens=400,  # room for a real little conversation plus a short script
    )
    return _clean(result.parsed, state, objs)
