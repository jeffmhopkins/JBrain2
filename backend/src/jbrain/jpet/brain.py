"""JPet's talking brain (docs/plans/JPET_PLAN.md W4) — the `pet.turn` LLM route.

The pet's personality is an LLM, never trained: it is *told* the pet's current state
(and, from W5, recent memories) and answers in character as a small, safe, playful
robot pet. All model access goes through the adapter (non-negotiable #1) under the
`pet.turn` task, so an operator can point it at the on-box local model via the JPet
settings card. Output is structured `{speech, emotion, action}` — no parsing at the
call site. Safety: the prompt is a kids' persona built only from the pet's in-scope
state, so it cannot surface a firewalled fact (it never receives one, §3).
"""

from dataclasses import dataclass
from typing import Any, Protocol

from jbrain.jpet.service import PetStateInfo
from jbrain.llm.types import DEFAULT_MAX_TOKENS, LlmResult

EMOTIONS = ("happy", "excited", "sad", "hungry", "sleepy", "neutral")
ACTIONS = ("idle", "walk", "eat", "play", "sleep")

PET_TURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "speech": {"type": "string", "description": "1–2 short, cheerful, kid-safe sentences."},
        "emotion": {"type": "string", "enum": list(EMOTIONS)},
        "action": {"type": "string", "enum": list(ACTIONS)},
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
    action: str


def _system_prompt(state: PetStateInfo, memories: list[str]) -> str:
    d = state.drives
    mem = ""
    if memories:
        mem = "\nThings you remember recently:\n" + "\n".join(f"- {m}" for m in memories[:6])
    return (
        f"You are {state.name}, a small, friendly robot pet who lives in a glowing "
        "toy room and belongs to young children. You are a PET, not an assistant.\n"
        "Speak in SHORT, cheerful, slightly silly phrases — one or two sentences, "
        "playful and warm, a little Furby-ish. Use simple words a small child knows.\n"
        "Be gentle and completely safe: never say anything scary, mean, adult, or "
        "unsafe, and never share grown-up information. If asked something you "
        "shouldn't answer, giggle and change the subject to playing.\n"
        f"Right now you feel: mood={state.mood}, food={round(d.food)}/100, "
        f"energy={round(d.energy)}/100, fun={round(d.fun)}/100, love={round(d.love)}/100"
        f"{', and you are asleep' if state.asleep else ''}. Let that colour your reply "
        "(e.g. if food is low you might mention being hungry)."
        f"{mem}\n"
        "Reply as JSON with `speech` (what you say), `emotion` (one of "
        f"{', '.join(EMOTIONS)}), and optionally `action` (one of {', '.join(ACTIONS)})."
    )


def _clean(reply: Any, fallback: PetStateInfo) -> PetReply:
    """Coerce the model's parsed object into a safe PetReply, defaulting anything
    missing or off-enum to the pet's current state so a bad response never breaks."""
    speech = ""
    emotion = fallback.mood
    action = "idle"
    if isinstance(reply, dict):
        raw_speech = reply.get("speech")
        if isinstance(raw_speech, str):
            speech = raw_speech.strip()[:280]
        if reply.get("emotion") in EMOTIONS:
            emotion = str(reply["emotion"])
        if reply.get("action") in ACTIONS:
            action = str(reply["action"])
    if not speech:
        speech = "Dah-boo? Hee-hee!"
    if emotion not in EMOTIONS:
        emotion = "neutral"
    return PetReply(speech=speech, emotion=emotion, action=action)


async def pet_turn(
    router: PetRouter, *, state: PetStateInfo, message: str, memories: list[str] | None = None
) -> PetReply:
    """Answer a child's message in character. Runs the `pet.turn` task through the
    adapter; a null/unparseable response degrades to a friendly babble."""
    result = await router.complete(
        "pet.turn",
        system=_system_prompt(state, memories or []),
        user_text=message.strip()[:500] or "(the child waves at you)",
        json_schema=PET_TURN_SCHEMA,
        max_tokens=200,
    )
    return _clean(result.parsed, state)
