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


# The wall's field "build a statue of X". The LLM DESIGNS the subject as a handful of solid
# coloured PRIMITIVES (box/ellipsoid/cylinder/cone) on a 24³ grid; the host voxelizes them into
# cubes here. This beats asking the model to hand-place hundreds of voxels: the geometry is code,
# so shapes come out solid, symmetric and clean (Minecraft-ish), the output is ~1/6 the tokens
# (seconds, not minutes), and the surface shell that the wall builds is computed correctly.
STATUE_GRID = 32
STATUE_MAX_PRIMS = 80
STATUE_MAX_VOXELS = 2400  # the (shelled) voxel budget the wall builds; a safety cap, rarely hit
_PRIM_TYPES = ("box", "ellipsoid", "cylinder", "cone")
STATUE_PRIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "primitives": {
            "type": "array",
            "maxItems": STATUE_MAX_PRIMS,
            "description": "The solid shapes the model is built from.",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(_PRIM_TYPES)},
                    "cx": {"type": "number"},
                    "cy": {"type": "number"},
                    "cz": {"type": "number"},
                    "sx": {"type": "number"},
                    "sy": {"type": "number"},
                    "sz": {"type": "number"},
                    "axis": {"type": "string", "enum": ["x", "y", "z"]},
                    "c": {"type": "string", "description": 'Hex colour like "#8b5a2b".'},
                },
                "required": ["type", "cx", "cy", "cz", "sx", "sy", "sz", "c"],
            },
        }
    },
    "required": ["primitives"],
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
        "You are a 3D modeller for a children's toy that builds blocky 'statues' out of cubes "
        "(think Minecraft). You DESIGN a subject as a small set of simple solid SHAPES; a program "
        "then fills them with cubes, so you never place individual cubes — you place shapes.\n"
        f"Work in a {g}×{g}×{g} space, coordinates 0–{g - 1}. x = left↔right, z = front↔back, "
        f"y = UP. Rest the model on the ground (lowest shape touches y=0) and centre it in x and z "
        f"(around {g // 2}).\n"
        "Return `primitives`: a list of shapes. Each shape has: `type` "
        '("box" | "ellipsoid" | "cylinder" | "cone"); `cx,cy,cz` the CENTRE (may be fractional); '
        '`sx,sy,sz` the full SIZE along x, y, z; `axis` ("x"|"y"|"z") the long axis for '
        "cylinder/cone (a cone tapers to a point at the +axis end; ignored for box/ellipsoid); and "
        '`c` a hex colour like "#8b5a2b".\n'
        "Shape guide: box = cuboid body/roof; ellipsoid = sphere/egg head or belly (sx=sy=sz is a "
        "sphere); cylinder = leg/trunk/tube; cone = nose/beak/tail/roof-spike.\n"
        "Design rules: be RECOGNISABLE and BOLD — a 4-year-old names it instantly, silhouette "
        "first. Use bilateral symmetry: give mirror pairs (two legs, two ears, two eyes, two "
        "wings) equal, opposite offsets from the centre. Colour each part with several natural, "
        "distinct colours (body, belly, face, eyes, nose, wheels, windows…). Keep it to roughly "
        "8–40 shapes; overlaps are fine (later shapes paint over earlier)."
    )


def _hex_color(value: Any) -> str:
    """Normalise a colour to "#rrggbb"; a #rgb shorthand expands, anything unusable → grey."""
    if isinstance(value, str):
        s = value.strip().lstrip("#").lower()
        if len(s) == 3 and all(ch in "0123456789abcdef" for ch in s):
            s = "".join(ch * 2 for ch in s)
        if len(s) == 6 and all(ch in "0123456789abcdef" for ch in s):
            return f"#{s}"
    return "#9aa0b0"


def _prim_contains(p: dict[str, float], x: int, y: int, z: int) -> bool:
    """Whether grid cell (x,y,z) lies inside primitive p. Box/ellipsoid ignore axis; cylinder is
    an (elliptical) tube along its axis; cone is that tube tapering to a point at the +axis end."""
    t, dx, dy, dz = p["type"], x - p["cx"], y - p["cy"], z - p["cz"]
    sx, sy, sz = max(p["sx"], 1e-3), max(p["sy"], 1e-3), max(p["sz"], 1e-3)
    if t == "box":
        return abs(dx) <= sx / 2 and abs(dy) <= sy / 2 and abs(dz) <= sz / 2
    if t == "ellipsoid":
        return (dx / (sx / 2)) ** 2 + (dy / (sy / 2)) ** 2 + (dz / (sz / 2)) ** 2 <= 1.05
    axis = p.get("axis", "y")
    if axis == "x":
        a, la, r1, r2, s1, s2 = dx, sx, dy, dz, sy, sz
    elif axis == "z":
        a, la, r1, r2, s1, s2 = dz, sz, dx, dy, sx, sy
    else:
        a, la, r1, r2, s1, s2 = dy, sy, dx, dz, sx, sz
    if abs(a) > la / 2:
        return False
    rr1, rr2 = s1 / 2, s2 / 2
    if t == "cone":  # taper from full radius at the base (−axis) to a point at the apex (+axis)
        scale = max(1 - (a + la / 2) / la, 1e-3)
        rr1, rr2 = rr1 * scale, rr2 * scale
    return (r1 / max(rr1, 1e-3)) ** 2 + (r2 / max(rr2, 1e-3)) ** 2 <= 1.05


def _clean_primitives(parsed: Any) -> list[dict[str, Any]]:
    """Keep well-formed, in-type primitives with numeric geometry and a usable colour."""
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("primitives")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:STATUE_MAX_PRIMS]:
        if not isinstance(item, dict) or item.get("type") not in _PRIM_TYPES:
            continue
        try:
            p: dict[str, Any] = {k: float(item[k]) for k in ("cx", "cy", "cz", "sx", "sy", "sz")}
        except (KeyError, TypeError, ValueError):
            continue
        p["type"] = item["type"]
        p["axis"] = item["axis"] if item.get("axis") in ("x", "y", "z") else "y"
        p["c"] = _hex_color(item.get("c"))
        out.append(p)
    return out


def voxelize(primitives: list[dict[str, Any]]) -> list[Voxel]:
    """Fill the primitives with cubes on the grid (later shapes paint over earlier), then keep
    only the SURFACE shell (a cell missing any of its 6 neighbours) — the buildable, hollow model.
    Iterating each primitive's bounding box keeps this quick."""
    g = STATUE_GRID
    grid: dict[tuple[int, int, int], str] = {}
    for p in primitives:
        lo_x, hi_x = int(p["cx"] - p["sx"] / 2), int(p["cx"] + p["sx"] / 2) + 1
        lo_y, hi_y = int(p["cy"] - p["sy"] / 2), int(p["cy"] + p["sy"] / 2) + 1
        lo_z, hi_z = int(p["cz"] - p["sz"] / 2), int(p["cz"] + p["sz"] / 2) + 1
        for x in range(max(0, lo_x), min(g, hi_x + 1)):
            for y in range(max(0, lo_y), min(g, hi_y + 1)):
                for z in range(max(0, lo_z), min(g, hi_z + 1)):
                    if _prim_contains(p, x, y, z):
                        grid[(x, y, z)] = p["c"]
    out: list[Voxel] = []
    for (x, y, z), c in grid.items():
        buried = all(
            (x + dx, y + dy, z + dz) in grid
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
        )
        if not buried:
            out.append(Voxel(x=x, y=y, z=z, c=c))
        if len(out) >= STATUE_MAX_VOXELS:
            break
    return out


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
    """Design `subject` as coloured primitives via the LLM, then voxelize them here into the
    hollow-shell model the wall builds. Runs the reasoning-bound `pet.statue` task. Raises on an
    unusable (no primitives / empty) model so the caller can tell the wall it couldn't imagine."""
    result = await router.complete(
        "pet.statue",
        system=_statue_system_prompt(),
        user_text=(subject.strip()[:80] or "a friendly robot"),
        json_schema=STATUE_PRIM_SCHEMA,
        # A handful of primitives is tiny output; budget mainly for the reasoning trace.
        max_tokens=8000,
    )
    voxels = voxelize(_clean_primitives(result.parsed))
    if not voxels:
        raise ValueError("statue model was empty")
    return voxels
