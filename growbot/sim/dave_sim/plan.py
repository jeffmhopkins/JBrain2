"""The floor plan: where the robot went, drawn from above as an SVG, and the numbers a
scenario scores a wander by. Cheap enough for every run: a ten-minute wander in the house
replays nothing, where the onlooker video re-renders every pose.

The path is coloured by speed over the last second, arrows show which way his camera faced
every few seconds, and each reply that said something is a numbered dot where he was when
it reached the page.
"""

from __future__ import annotations

import html
import json
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dave_sim.run import Sim

PX = 110  # pixels per metre
PAD = 16
# Speed scale's top, cm/s: the trained forward walks about 3 to 5 in the sim.
TOP_CM_S = 8.0
ARROW_EVERY_S = 6.0
ROOM = (-3.0, -3.0, 3.0, 3.0)  # the bare room's walls


def _house(sim: Sim) -> Any:
    return sim.sb.house if sim.o.scene == "house" else None


def track(sim: Sim) -> list[dict[str, float]]:
    """(t, x, y, facing, speed) along the run; facing is where the camera looked (degrees
    from +x), speed the last second's straight-line distance in cm/s."""
    out: list[dict[str, float]] = []
    states = sim.states
    j = 0
    for s in states:
        while j < len(states) and states[j]["wall"] < s["wall"] - 1.0:
            j += 1
        a = states[j]
        dt = s["wall"] - a["wall"]
        sp = math.hypot(s["x"] - a["x"], s["y"] - a["y"]) / dt * 100 if dt > 0.3 else 0.0
        face = (s["nose_deg"] + 180.0 + 180.0) % 360.0 - 180.0
        out.append({"t": s["wall"], "x": s["x"], "y": s["y"], "face": face, "speed": sp})
    return out


def stats(sim: Sim) -> dict[str, Any]:
    """How far he went, which rooms he was in and for how long, and how close he came to
    each piece of furniture."""
    tr = track(sim)
    steps = zip(tr, tr[1:], strict=False)
    dist = sum(math.hypot(b["x"] - a["x"], b["y"] - a["y"]) for a, b in steps)
    out: dict[str, Any] = {"walked_m": round(dist, 2)}
    if tr:
        a, b = tr[0], tr[-1]
        out["from_start_m"] = round(math.hypot(b["x"] - a["x"], b["y"] - a["y"]), 2)
    h = _house(sim)
    if h is None or not tr:
        return out
    rooms: dict[str, float] = {}
    for a, b in zip(tr, tr[1:], strict=False):
        r = h.room_at(a["x"], a["y"]) or "outside"
        rooms[r] = rooms.get(r, 0.0) + (b["t"] - a["t"])
    out["rooms_s"] = {k: round(v) for k, v in rooms.items()}
    out["closest_m"] = {
        it.name: round(min(h.distance_to(it.name, p["x"], p["y"]) for p in tr), 2)
        for it in h.ITEMS
        if it.name != "rug"
    }
    return out


def _speed_rgb(cm_s: float) -> str:
    """Still is a cool grey-blue, a full stride a warm red."""
    f = max(0.0, min(1.0, cm_s / TOP_CM_S))
    lo, mid, hi = (70, 110, 170), (235, 180, 40), (200, 40, 40)
    a, b, g = (lo, mid, f * 2) if f < 0.5 else (mid, hi, f * 2 - 1)
    return "rgb({},{},{})".format(*(round(x + (y - x) * g) for x, y in zip(a, b, strict=True)))


def svg(sim: Sim) -> str:
    h = _house(sim)
    x0, y0, x1, y1 = h.OUTER if h is not None else ROOM
    w, ht = (x1 - x0) * PX + 2 * PAD, (y1 - y0) * PX + 2 * PAD

    def P(x: float, y: float) -> tuple[float, float]:
        return round(PAD + (x - x0) * PX, 1), round(PAD + (y1 - y) * PX, 1)

    el: list[str] = []
    ax, ay = P(x0, y1)
    el.append(
        f'<rect x="{ax}" y="{ay}" width="{(x1 - x0) * PX}" height="{(y1 - y0) * PX}" '
        'fill="#f6f3ec" stroke="#555" stroke-width="3"/>'
    )
    if h is not None:
        for name, (rx0, ry0, rx1, ry1) in h.ROOMS.items():
            fill = "#efe6d2" if name == "living room" else "#dde9ee"
            px, py = P(rx0, ry1)
            el.append(
                f'<rect x="{px}" y="{py}" width="{(rx1 - rx0) * PX}" height="{(ry1 - ry0) * PX}" '
                f'fill="{fill}" stroke="#555" stroke-width="2"/>'
            )
            el.append(
                f'<text x="{px + 6}" y="{py + 16}" font-size="12" fill="#666">{name}</text>'
            )
        # The doorway, as a gap in the dividing wall.
        dx, dy0 = P(h.DIVIDE_X, h.DOOR[1])
        el.append(
            f'<line x1="{dx}" y1="{dy0}" x2="{dx}" y2="{dy0 + (h.DOOR[1] - h.DOOR[0]) * PX}" '
            'stroke="#fff" stroke-width="5"/>'
        )
        for it in h.ITEMS:
            cx, cy = P(it.x, it.y)
            r, g, b = it.rgb
            el.append(
                f'<rect x="{cx - it.hx * PX}" y="{cy - it.hy * PX}" width="{2 * it.hx * PX}" '
                f'height="{2 * it.hy * PX}" fill="rgb({r},{g},{b})" fill-opacity="0.75" '
                'stroke="#333" stroke-width="1"/>'
            )
            el.append(
                f'<text x="{cx}" y="{cy + 4}" font-size="11" text-anchor="middle" '
                f'fill="#000" stroke="#fff" stroke-width="3" paint-order="stroke">{it.name}</text>'
            )
    # Every half second is plenty for a line, and keeps a ten-minute wander's SVG small.
    tr = track(sim)[::6]
    for a, b in zip(tr, tr[1:], strict=False):
        (pa, qa), (pb, qb) = P(a["x"], a["y"]), P(b["x"], b["y"])
        el.append(
            f'<line x1="{pa}" y1="{qa}" x2="{pb}" y2="{qb}" stroke="{_speed_rgb(b["speed"])}" '
            'stroke-width="4" stroke-linecap="round"/>'
        )
    last = -1e9
    for p in tr:
        if p["t"] - last < ARROW_EVERY_S:
            continue
        last = p["t"]
        px, py = P(p["x"], p["y"])
        a = math.radians(p["face"])
        qx, qy = px + 22 * math.cos(a), py - 22 * math.sin(a)
        el.append(
            f'<line x1="{px}" y1="{py}" x2="{round(qx, 1)}" y2="{round(qy, 1)}" stroke="#222" '
            'stroke-width="1.5" marker-end="url(#ah)"/>'
        )
    if tr:
        sx, sy = P(tr[0]["x"], tr[0]["y"])
        el.append(f'<circle cx="{sx}" cy="{sy}" r="6" fill="none" stroke="#000" stroke-width="2"/>')
    for n, (t, said) in enumerate(replies(sim), 1):
        p = min(tr, key=lambda q: abs(q["t"] - t)) if tr else None
        if p is None:
            break
        px, py = P(p["x"], p["y"])
        el.append(
            f'<g><title>{html.escape(f"{t:.0f} s: {said}")}</title>'
            f'<circle cx="{px}" cy="{py}" r="8" fill="#fff" stroke="#1d4ed8" stroke-width="2"/>'
            f'<text x="{px}" y="{py + 4}" font-size="10" text-anchor="middle" '
            f'fill="#1d4ed8">{n}</text></g>'
        )
    legend = "".join(
        f'<rect x="{PAD + i * 18}" y="{ht + 4}" width="18" height="10" fill="{_speed_rgb(v)}"/>'
        for i, v in enumerate(range(0, int(TOP_CM_S) + 1))
    )
    legend += (
        f'<text x="{PAD + (int(TOP_CM_S) + 1) * 18 + 6}" y="{ht + 13}" font-size="11">'
        f"speed 0 to {TOP_CM_S:.0f}+ cm/s; arrows: where his camera faced, every "
        f"{ARROW_EVERY_S:.0f} s; numbered dots: what he said there</text>"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {ht + 22}" width="100%">'
        '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" '
        'markerHeight="5" orient="auto"><path d="M0,0L10,5L0,10z" fill="#222"/></marker></defs>'
        + "".join(el)
        + legend
        + "</svg>"
    )


def replies(sim: Sim) -> list[tuple[float, str]]:
    """Each reply that said something, with when it reached the page."""
    out = []
    for e in sorted(sim.timeline, key=lambda e: e["t"]):
        if e["kind"] != "dave" or e.get("warm"):
            continue
        try:
            r = json.loads(e["text"])
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("say") and "dream_say" not in r:
            out.append((e["t"], str(r["say"])))
    return out
