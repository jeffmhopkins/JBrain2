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


# The wall's field "build a statue of X": the LLM sculpts a recognisable shape as a sparse
# list of coloured voxels on a 24³ grid (y up, resting on y=0). Kept sparse (occupied cells
# only) + capped so the wire stays small and the wall's block-by-block build terminates.
STATUE_GRID = 24
STATUE_MAX_VOXELS = 1200
STATUE_VOXEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "voxels": {
            "type": "array",
            "maxItems": STATUE_MAX_VOXELS,
            "description": "The occupied cells of the model — one entry per filled voxel.",
            "items": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0, "maximum": STATUE_GRID - 1},
                    "y": {"type": "integer", "minimum": 0, "maximum": STATUE_GRID - 1},
                    "z": {"type": "integer", "minimum": 0, "maximum": STATUE_GRID - 1},
                    "c": {"type": "string", "description": 'Hex colour like "#ff8800".'},
                },
                "required": ["x", "y", "z", "c"],
            },
        }
    },
    "required": ["voxels"],
}


@dataclass(frozen=True)
class Voxel:
    x: int
    y: int
    z: int
    c: str  # a normalised "#rrggbb" colour


def _statue_system_prompt() -> str:
    g = STATUE_GRID
    return (
        "You are a voxel sculptor for a children's toy: you turn a subject into a small, "
        f"instantly-recognisable 3D model on a {g}×{g}×{g} grid of cubes.\n"
        f"Coordinates are integers 0–{g - 1}. x = left→right, z = front→back, y = UP. The model "
        "must sit on the ground (fill from y=0 up) and be roughly centred in x and z.\n"
        "Return ONLY the occupied cells as `voxels`: a list of {x, y, z, c}, where c is a hex "
        'colour like "#ff8800".\n'
        "COLOUR IT PROPERLY: use SEVERAL distinct hex colours to pick out the parts — e.g. body vs "
        "belly, ears, eyes, nose, stripes, wheels, windows. At least 3–4 different colours; a "
        "single flat colour looks wrong. Choose natural colours for the subject.\n"
        "Make it a solid, chunky, recognisable shape a 4-year-old would name at a glance — bold "
        "silhouette over fine detail. Do not fill the whole grid; carve the actual shape.\n"
        "IMPORTANT — build a HOLLOW SHELL: emit only the OUTER surface voxels (the ones you could "
        "see or touch from outside), about 1 voxel thick, and leave the inside empty. A voxel is "
        "interior (skip it) when it has a filled neighbour on all six sides.\n"
        "Keep it SMALL so it's quick to make: aim for roughly 120–300 shell voxels (a coarse, "
        f"chunky model is perfect — do not over-detail); never exceed {STATUE_MAX_VOXELS}."
    )


def _clean_voxels(parsed: Any) -> list[Voxel]:
    """Coerce the model's parsed object into grid-valid voxels: keep only in-bounds integer
    cells with a usable hex colour, de-duplicate by cell, and cap the count."""
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("voxels")
    if not isinstance(raw, list):
        return []
    seen: set[tuple[int, int, int]] = set()
    out: list[Voxel] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            x, y, z = int(item["x"]), int(item["y"]), int(item["z"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= x < STATUE_GRID and 0 <= y < STATUE_GRID and 0 <= z < STATUE_GRID):
            continue
        cell = (x, y, z)
        if cell in seen:
            continue
        seen.add(cell)
        out.append(Voxel(x=x, y=y, z=z, c=_hex_color(item.get("c"))))
        if len(out) >= STATUE_MAX_VOXELS:
            break
    return out


def _hex_color(value: Any) -> str:
    """Normalise a colour to "#rrggbb"; a #rgb shorthand expands, anything unusable → grey."""
    if isinstance(value, str):
        s = value.strip().lstrip("#").lower()
        if len(s) == 3 and all(ch in "0123456789abcdef" for ch in s):
            s = "".join(ch * 2 for ch in s)
        if len(s) == 6 and all(ch in "0123456789abcdef" for ch in s):
            return f"#{s}"
    return "#9aa0b0"


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
        # A local reasoning model (e.g. Qwen3.5-4B) emits ~1.2k tokens of thinking BEFORE the
        # closing JSON even at low effort; a tight cap truncates the reply mid-thought, so the
        # JSON never closes → invalid → the turn falls back to a canned line. Budget for the
        # thinking + the JSON so the real answer actually lands. (Snappier still: route pet.turn
        # at "none" reasoning, which drops the thinking entirely.)
        max_tokens=2048,
    )
    return _clean(result.parsed, state, objs)


async def statue_voxels(router: PetRouter, *, subject: str) -> list[Voxel]:
    """Ask the LLM to sculpt `subject` as a 24³ voxel model for the wall's field build. Runs
    the reasoning-bound `pet.statue` task; the result is validated + clamped to the grid so a
    bad cell can never wedge the wall's builder. Raises on an unusable (empty) model so the
    caller can tell the wall it couldn't imagine that one."""
    result = await router.complete(
        "pet.statue",
        system=_statue_system_prompt(),
        user_text=(subject.strip()[:80] or "a friendly robot"),
        json_schema=STATUE_VOXEL_SCHEMA,
        # A big, reasoning-heavy generation: budget for a long thinking trace PLUS a few hundred
        # voxels of JSON, or the model truncates mid-list and the parse yields a half-built shape.
        max_tokens=32000,
    )
    voxels = _clean_voxels(result.parsed)
    if not voxels:
        raise ValueError("statue model was empty")
    return voxels
