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

import json
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
# coloured PRIMITIVES (box/ellipsoid/cylinder/cone) on a STATUE_GRID³ grid; the host voxelizes
# them into cubes here. This beats asking the model to hand-place hundreds of voxels: the geometry
# is code, so shapes come out solid, symmetric and clean (Minecraft-ish), the output is ~1/6 the
# tokens (seconds, not minutes), and the surface shell that the wall builds is computed correctly.
# The grid is 48³ (up from 32³): a finer, larger build. Voxels are the SURFACE SHELL, so they scale
# ~with grid², hence the raised cap. The wall builds on a fixed ~90s schedule regardless of count.
STATUE_GRID = 48
STATUE_MAX_PRIMS = 80
STATUE_MAX_VOXELS = 6000  # the (shelled) voxel budget the wall builds; a safety cap sized for 48³
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


# Two worked examples authored on a 32-unit grid, then scaled to STATUE_GRID when the prompt is
# built so their coordinates always match the live grid. A concrete, correctly-proportioned,
# box-dominated example anchors the model's sense of scale and structure far better than prose
# alone (one-shot demonstration is the single biggest quality lever for this task). Each tuple is
# (type, cx, cy, cz, sx, sy, sz, colour).
_PIG_EXAMPLE = (
    ("box", 16, 10, 16, 10, 8, 16, "#f0a0a0"),
    ("box", 12, 3, 11, 4, 6, 4, "#e58f8f"),
    ("box", 20, 3, 11, 4, 6, 4, "#e58f8f"),
    ("box", 12, 3, 21, 4, 6, 4, "#e58f8f"),
    ("box", 20, 3, 21, 4, 6, 4, "#e58f8f"),
    ("box", 16, 11, 26, 8, 8, 8, "#f2a6a6"),
    ("box", 16, 9, 30, 5, 4, 2, "#f8c4c4"),
    ("box", 14, 13, 30, 1, 1, 1, "#1c130c"),
    ("box", 18, 13, 30, 1, 1, 1, "#1c130c"),
    ("box", 13, 15, 25, 2, 3, 2, "#e08787"),
    ("box", 19, 15, 25, 2, 3, 2, "#e08787"),
    ("box", 16, 11, 8, 2, 2, 3, "#e79a9a"),
)
_MONKEY_EXAMPLE = (
    ("box", 16, 12, 15, 8, 10, 6, "#7a5233"),
    ("box", 13, 4, 16, 4, 8, 5, "#6b4a2f"),
    ("box", 19, 4, 16, 4, 8, 5, "#6b4a2f"),
    ("box", 16, 21, 16, 8, 8, 8, "#7a5233"),
    ("box", 16, 20, 20, 6, 5, 1, "#e0c19a"),
    ("box", 14, 22, 20, 1, 1, 1, "#1c130c"),
    ("box", 18, 22, 20, 1, 1, 1, "#1c130c"),
    ("box", 11, 21, 16, 2, 3, 3, "#e0c19a"),
    ("box", 21, 21, 16, 2, 3, 3, "#e0c19a"),
    ("box", 12, 17, 18, 3, 9, 3, "#6b4a2f"),
    ("box", 20, 17, 18, 3, 9, 3, "#6b4a2f"),
    ("box", 16, 19, 22, 6, 2, 2, "#f2ce3a"),
    ("box", 16, 10, 9, 2, 2, 7, "#6b4a2f"),
)


def _example_json(prims: tuple[tuple[Any, ...], ...], s: float, g: int) -> str:
    """Serialise a base-32 worked example scaled to the live grid, one primitive per line."""
    rows = []
    for t, cx, cy, cz, sx, sy, sz, c in prims:
        row = {
            "type": t,
            "cx": max(0, min(g - 1, round(cx * s))),
            "cy": max(0, min(g - 1, round(cy * s))),
            "cz": max(0, min(g - 1, round(cz * s))),
            "sx": max(1, round(sx * s)),
            "sy": max(1, round(sy * s)),
            "sz": max(1, round(sz * s)),
            "c": c,
        }
        rows.append(json.dumps(row, separators=(",", ":")))
    return '{"primitives":[\n ' + ",\n ".join(rows) + "\n]}"


def _statue_system_prompt() -> str:
    g = STATUE_GRID
    s = g / 32.0  # the examples and size hints are authored on a 32-grid; scale to the live grid
    c = g // 2
    r = lambda n: max(1, round(n * s))  # noqa: E731 — round a base-32 size hint to the live grid
    return (
        "You are a 3D modeller for a children's toy that builds blocky 'statues' out of cubes, in "
        "the style of MINECRAFT mobs and LEGO. Everything is made of rectangular BLOCKS — cubes, "
        "prisms and square posts — with hard 90° edges. You DESIGN a subject as a set of solid "
        "SHAPES; a program fills them with cubes, so you never place cubes — you place shapes. "
        "Make it DETAILED, COLOURFUL and full of character — a 4-year-old should grin and name it "
        "instantly.\n"
        "THE BLOCKY RULE (most important): use `box` for almost everything — about 85% of your "
        "shapes must be boxes, and a mostly-box model is perfect. A Minecraft head is a CUBE, not "
        "a ball; a leg is a square POST, not a tube; a snout, ear or tail is a BOX, not a cone.\n"
        " - `cone`: ONLY for a genuine spike or point — a unicorn horn, a dragon's back spikes, "
        "claws, a tongue of fire, a party hat, a carrot nose. Never for a snout, ear, foot or "
        "tail.\n"
        " - `cylinder`: ONLY for a truly round tube — a cannon barrel, a wheel, a mug. Never for "
        "legs, necks or bodies.\n"
        " - `ellipsoid`: avoid; only for an unmistakable smooth ball (a single eyeball).\n"
        " COMMON MISTAKE TO AVOID: making the model look rounded or 'blobby'. This is a BLOCKY toy "
        "— cubes, edges and corners are the whole point. A rounded cat is WRONG; a cubic, angular "
        "cat is RIGHT. When in doubt, use `box`.\n"
        f"SPACE: a {g}×{g}×{g} grid, integer coordinates 0–{g - 1}. x = left↔right, z = "
        "front↔back, y = UP. Use whole numbers. The subject faces +z (front). Rest it on the "
        "ground: the lowest shape's bottom sits at y=0 (a shape of height sy centred at cy = sy/2 "
        f"touches the floor). Centre the main mass near x={c}, z={c} and build BIG — fill most of "
        f"the {g} cube so there is room for detail. Don't build a tiny model in the middle.\n"
        "STANCE — stand it UP on its legs. Unless the subject is clearly lying down, sitting or "
        "crouching, the LEGS reach the floor (their bottoms at y=0) and LIFT the body so there is "
        "clear OPEN SPACE under the belly — you should see daylight between the four legs. Do NOT "
        "rest the body or belly on the ground with stubby legs. For a standing animal, make legs "
        f"about {r(8)}–{r(12)} cubes tall and put the body's bottom at the TOP of the legs (e.g. "
        f"legs y0–{r(10)}, body starts at y{r(10)}), so the creature stands tall, not squashed "
        "onto the floor.\n"
        "PROPORTION: a four-legged animal, car, bus or boat is LONGER front-to-back (z) than WIDE "
        "side-to-side (x) — make its body long in z and narrow in x (body sz > sx). An upright "
        "biped (person, monkey, penguin, standing dragon) is the exception: it is TALL, and its "
        "shoulders may be a bit wider than it is deep. Only make something wide in x if it truly "
        "is (a sofa).\n"
        "METHOD — build body-first, attach everything else relative to it:\n"
        " 1. BODY: place the main body as one box FIRST; decide its size and centre.\n"
        " 2. HEAD: a cube at the FRONT (high z), near the top of the body.\n"
        " 3. LIMBS: four separate leg boxes at the body's bottom corners for a quadruped — leave a "
        "clear GAP between the legs so they read as legs, not a solid base; two legs for a bird or "
        "biped; arms/wings as needed.\n"
        " 4. TAIL off the back, then the DETAIL (below).\n"
        f" Parts in left/right pairs (legs, arms, ears, eyes, wings) MUST mirror across x={c}: a "
        f"left leg at x={r(12)} means a right leg at x={r(20)} — never a single part on the "
        "centreline. Every part should touch or overlap its neighbour (overlaps are fine — later "
        "shapes paint over earlier).\n"
        "DETAIL & CHARACTER — this is what makes it good, not just a blocky lump. Use 15–45 shapes "
        "and spend most of them here:\n"
        " - Give it a FACE: two small eye boxes (dark), a nose or muzzle box (often paler, on the "
        "head front), maybe a mouth box. A face is what turns a brown box into an animal.\n"
        " - EXAGGERATE the 1–2 signature features that name the subject: a reindeer's tall "
        "antlers, a rabbit's long ears, a cat's triangle ears and long tail, an elephant's trunk, "
        "a fox's bushy tail. Build these signature parts BIG: a trunk, tail, neck, horn, ear or "
        f"held prop should be LONG and THICK — roughly {r(8)}–{r(14)} cubes long and {r(3)}–{r(5)} "
        "thick — never a small stub. If the subject's fame rests on one part (the elephant's "
        "trunk, the giraffe's neck), make it the boldest thing on the model.\n"
        " - Add COLOUR MARKINGS as their own boxes: a lighter belly and paws, tabby stripes, a "
        "dark nose, spots, a mane. Use a RICH palette — a main colour, a lighter underside, dark "
        "eyes, a pink/black nose, and bright colours for anything special. Never leave it one flat "
        "brown.\n"
        "POSE, PROPS & ACTION — if the subject is DOING something, show it:\n"
        " - Pose the limbs: raise the arms/paws to the mouth for eating or drinking; rear up; sit; "
        "spread wings to fly; bend a leg for a step.\n"
        " - Build the PROP as its own coloured boxes, make it BIG and BOLD (about as large as the "
        "head), and place it RIGHT AT THE ACTION: food goes directly in FRONT of the mouth — just "
        f"beyond the head's front face, centred on x={c}, at mouth height — with the hands/paws "
        "brought up around it. A held ball sits in the paws; a hat sits ON the head. Don't tuck "
        "the prop off to the side or make it a tiny nub.\n"
        " - Keep the subject the star: pose and prop, but the creature is still the biggest, "
        "boldest mass.\n"
        "FANTASY & MADE-UP creatures — assemble them from familiar parts, boldly:\n"
        " - A UNICORN is a horse: long body, four legs, a head on a short NECK at the front, plus "
        "a single `cone` HORN pointing UP-and-forward from the forehead and a bright RAINBOW mane "
        "running down the back of the neck and a rainbow tail.\n"
        " - A DRAGON: a long body; a long NECK rising to a head at the FRONT; a long TAIL at the "
        "back; four short legs; two BAT-WINGS as tall thin slabs that stand UP and back from the "
        "shoulders (NOT flat out to the sides like an aeroplane); a row of small `cone` or box "
        "SPIKES along the spine. To breathe FIRE, build a big bold plume of orange, yellow and red "
        "boxes and `cone`s shooting FORWARD out of the open mouth (increasing z, getting bigger as "
        "it goes) — keep the fire at the mouth and separate from the back spikes.\n"
        "OTHER SUBJECTS — not everything is an animal:\n"
        " - A PERSON is an upright biped: a box torso, a cube head with a FACE (eyes, nose, mouth) "
        "and HAIR, two arms and two legs, and CLOTHES in distinct colours (shirt, trousers, shoes, "
        "hat). For an ACTIVITY, pose the arms and legs and add the gear as its own boxes: a guitar "
        "(a big coloured box body + a long thin neck box) held across the chest; a bicycle (two "
        "`cylinder` or box WHEELS, a frame, handlebars) with the person seated and legs bent to "
        "the pedals; a ball at a kicking foot; an astronaut's white suit, helmet box and backpack. "
        "The person stays the biggest mass; the gear is bold and placed where the hands/feet use "
        "it.\n"
        " - A VEHICLE or BUILDING or OBJECT is built from big boxes and needs no face or legs: a "
        "CAR is a long low body box with a smaller cabin box on top, `cylinder` or dark-box WHEELS "
        "at the four lower corners, and window/light boxes; a HOUSE is a box with a triangular "
        "`box`/prism ROOF, a door box and window boxes; a ROCKET is a tall box body with a `cone` "
        "NOSE and fins; a CAKE is stacked layers with candle boxes. Use bright, distinct colours.\n"
        'COLOUR: each part one flat colour (`c`, a hex like "#8b5a2b"); no gradients.\n'
        "EXAMPLE 1 — a PIG (copy this box-first STRUCTURE, but design the subject you are actually "
        "asked for):\n" + _example_json(_PIG_EXAMPLE, s, g) + "\n"
        "(Body; four leg posts at the corners; a cube head at the front with a paler snout, two "
        "eye boxes and two ears; a tail — all cuboids.)\n"
        "EXAMPLE 2 — a MONKEY EATING A BANANA (shows a POSED biped with a FACE and a PROP):\n"
        + _example_json(_MONKEY_EXAMPLE, s, g)
        + "\n"
        "(Upright torso; two legs; a cube head with a pale muzzle, two eyes and two ears; two arms "
        "raised to the mouth; a YELLOW BANANA box held at the mouth; a long tail.)\n"
        "BEFORE YOU FINISH, check: nearly every shape is a `box` (cones only for real "
        f"spikes/horns/fire); all coordinates and sizes are whole numbers 0–{g - 1}; the lowest "
        "part touches y=0; a standing animal stands ON its legs with clear space under the belly "
        "(body NOT resting on the ground); a quadruped's body is longer in z than wide in x; "
        f"left/right pairs mirror across x={c}; it HAS A FACE and its signature feature is bold; "
        "if it is doing something, the pose is clear and the prop is BIG and right at the "
        "mouth/hands (not off to the side); wings (if any) stand UP not flat sideways, and fire "
        "shoots FORWARD from the mouth; 15–45 shapes; the silhouette is instantly the subject."
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
