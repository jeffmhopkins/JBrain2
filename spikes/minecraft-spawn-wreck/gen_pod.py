"""A jettisoned life-support / escape pod, small-house sized, lying where it came
down. Vanilla Bedrock block ids, Minecraft axes (x east, y up, z south).

Output JSON matches gen.py's (ship / terrain / figure / intact) plus "shots",
camera placements for render.py in Blender coordinates.
"""

import json
import math
import sys
from collections import Counter

import numpy as np

rng = np.random.default_rng(11)
_waves = [(rng.normal(0, 1, 3) * f, rng.uniform(0, 6.3)) for f in (0.25, 0.45, 0.8) for _ in range(3)]


def noise(x, y, z):
    return sum(math.sin(k[0] * x + k[1] * y + k[2] * z + ph) for k, ph in _waves) / 3.0


# ---------------------------------------------------------------- intact pod (local)
# axis along +x: heat shield at x<0, capsule cone 0..9, docking collar 9..12
def radius(x):
    if x < -1.2:
        return 0.0
    if x < 0:
        return 4.4 * math.sqrt(max(0.0, 1 - ((x + 0.0) / 1.25) ** 2)) if x < -0.6 else 4.4
    if x < 9:
        return 4.9 - 0.27 * x
    if x < 11.6:
        return 1.8
    return 0.0


pod, inside = {}, set()
for x in range(-2, 13):
    for y in range(-6, 7):
        for z in range(-6, 7):
            cx = x + 0.5
            r = math.hypot(y + 0.5, z + 0.5)
            R0 = radius(cx)
            if r > R0:
                continue
            ang = math.degrees(math.atan2(z + 0.5, y + 0.5)) % 360
            if cx < 0.6:
                # ablative heat shield: charred black, a grey rim where it meets the hull
                b = "gray_concrete" if r > R0 - 0.9 and cx > -0.6 else "black_concrete"
            elif cx >= 9:
                b = "gray_concrete" if r > 0.9 else "light_gray_concrete"
                if 10 <= cx < 10.6:
                    b = "light_gray_concrete"
                if cx >= 11 and r < 1.0:
                    b = "redstone_lamp"  # locator beacon
            elif r > R0 - 1.05:
                b = "white_concrete"
                if x in (3, 6):
                    b = "light_gray_concrete"
                if x == 7:
                    b = "orange_concrete"
                if x == 8 and int(ang) // 45 % 2 == 0:
                    b = "gray_concrete"  # RCS thruster quads
                if x in (2, 3, 4, 5) and abs(ang - 90) < 25 and not (x in (3, 4) and abs(ang - 90) < 13):
                    b = "black_concrete"  # hatch outline (the hatch side is +z)
                if x in (4, 5) and 20 < ang < 55:
                    b = "light_blue_stained_glass"  # windows
                if x in (4, 5) and 125 < ang < 160:
                    b = "light_blue_stained_glass"
            else:
                inside.add((x, y, z))
                continue
            pod[(x, y, z)] = b
intact = dict(pod)

# ---------------------------------------------------------------- crash pose
PITCH, ROLL, YAW = math.radians(8), 0.0, 0.0
cp, sp = math.cos(PITCH), math.sin(PITCH)
cr, sr = math.cos(ROLL), math.sin(ROLL)
cy_, sy_ = math.cos(YAW), math.sin(YAW)
R = (
    np.array([[cy_, 0, sy_], [0, 1, 0], [-sy_, 0, cy_]])
    @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    @ np.array([[cp, -sp, 0], [sp, cp, 0], [0, 0, 1]])
)
Rinv = R.T
GROUND = 64
# the cabin's middle sits 2.5 above grade, so a flat floor at grade has ~6 blocks
# of width and headroom inside
MID = np.array([4.5, 0.0, 0.0])
offset = np.array([0.0, GROUND + 2.0, 0.0]) - R @ MID
OFFS = [(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1)]


def resample(src, inner):
    pts = np.array(list(src.keys()), dtype=float) + 0.5
    w = (R @ pts.T).T + offset
    lo, hi = np.floor(w.min(0)) - 2, np.ceil(w.max(0)) + 2
    out, inn = {}, set()
    for x in range(int(lo[0]), int(hi[0])):
        for y in range(int(lo[1]), int(hi[1])):
            for z in range(int(lo[2]), int(hi[2])):
                lp = Rinv @ (np.array([x, y, z]) + 0.5 - offset) - 0.5
                base = np.round(lp).astype(int)
                best, bd, ind = None, 0.8, 9.0
                for o in OFFS:
                    q = (base[0] + o[0], base[1] + o[1], base[2] + o[2])
                    d = math.dist(q, lp)
                    if q in src and d < bd:
                        best, bd = src[q], d
                    elif q in inner and d < ind:
                        ind = d
                if best is not None and bd <= ind + 0.35:
                    out[(x, y, z)] = best
                elif math.hypot(lp[1] + 0.5, lp[2] + 0.5) < radius(lp[0] + 0.5) - 0.3:
                    # analytically inside the hull: cabin air, never terrain
                    inn.add((x, y, z))
    return out, inn


ship, interior = resample(pod, inside)

axis = R @ np.array([1.0, 0, 0])
axis_xz = np.array([axis[0], axis[2]]) / math.hypot(axis[0], axis[2])
side_xz = np.array([-axis_xz[1], axis_xz[0]])  # hatch faces this way
mid_w = R @ MID + offset
FLOOR = GROUND  # walkable level (feet) inside


def rel(p):
    d = np.array([p[0] + 0.5 - mid_w[0], p[2] + 0.5 - mid_w[2]])
    return float(d @ axis_xz), float(d @ side_xz)


# flat deck: everything inside below the feet level becomes floor
for p in list(interior):
    if p[1] < FLOOR:
        interior.discard(p)
        ship[p] = "smooth_stone" if p[1] == FLOOR - 1 else "polished_deepslate"

# hatch: an axis-aligned 2-wide, 2-tall opening through the side at deck level
hx, hz = (1, 0) if abs(side_xz[0]) > abs(side_xz[1]) else (0, 1)
sgn = int(np.sign(side_xz[0] if hx else side_xz[1]))
px, pz = (0, 1) if hx else (1, 0)  # along the wall
cxw, czw = int(math.floor(mid_w[0])), int(math.floor(mid_w[2]))
hatch_cells = []
for depth in range(0, 9):
    for w in (0, 1):
        for h in (0, 1):
            x = cxw + hx * sgn * depth + px * w
            z = czw + hz * sgn * depth + pz * w
            p = (x, FLOOR + h, z)
            hatch_cells.append(p)
            if p in ship:
                del ship[p]
                if depth < 6:
                    interior.add(p)
hatch_out = None
for depth in range(0, 12):
    p = (cxw + hx * sgn * depth, FLOOR, czw + hz * sgn * depth)
    if p not in interior and all((q not in ship) for q in [(p[0], p[1] + h, p[2]) for h in (0, 1)]) and depth > 2:
        hatch_out = p
        break
for p, b in list(ship.items()):  # frame: hull blocks bordering the opening
    if b == "white_concrete" or b == "light_gray_concrete":
        for q in hatch_cells:
            if abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2]) == 1:
                ship[p] = "iron_block"
                break

# ---------------------------------------------------------------- furnishing
floor_cells = [p for p in interior if p[1] == FLOOR and (p[0], FLOOR + 1, p[2]) in interior]
keep_clear = {(q[0], q[2]) for q in hatch_cells}
for q in list(keep_clear):  # one-cell aisle in from the hatch
    keep_clear.add((q[0] + px, q[1] + pz))
fc = [p for p in floor_cells if (p[0], p[2]) not in keep_clear]


def pick(score, n=1, taken=set()):
    ranked = sorted((c for c in fc if c not in taken), key=score)
    return ranked[:n]


taken = set()


def place(p, b):
    ship[p] = b
    interior.discard(p)
    taken.add(p)


# bed against the far wall, toward the nose
head = pick(lambda c: (rel(c)[1] * 3 - rel(c)[0]))[0]
place(head, "bed_head")
foot_dir = (-px, -pz) if True else (px, pz)
for d in ((px, pz), (-px, -pz), (hx, hz), (-hx, -hz)):
    q = (head[0] + d[0], FLOOR, head[2] + d[1])
    if q in fc and q not in taken:
        place(q, "bed_feet")
        break
# work bench + furnace at the shield end
wb = pick(lambda c: rel(c)[0] + 0.3 * abs(rel(c)[1]), taken=taken)[0]
place(wb, "crafting_table")
fu = pick(lambda c: rel(c)[0] + 0.3 * abs(rel(c)[1] - 1.5), taken=taken)[0]
place(fu, "furnace")
# supply barrels along the far wall, stacked two high in one spot
for c in pick(lambda c: rel(c)[1] * 2 + abs(rel(c)[0] + 1.5), 3, taken):
    place(c, "barrel")
    up = (c[0], FLOOR + 1, c[2])
    if up in interior and rng.random() < 0.6:
        ship[up] = "barrel"
        interior.discard(up)
# console under the windows at the nose end
for c in pick(lambda c: -rel(c)[0] + 0.5 * abs(rel(c)[1]), 2, taken):
    place(c, "black_concrete")
    up = (c[0], FLOOR + 1, c[2])
    if up in interior:
        ship[up] = "redstone_lamp"
        interior.discard(up)
# cabin light: the ceiling block above the middle
col = [p for p in ship if p[0] == cxw and p[2] == czw and p[1] > FLOOR + 2]
if col:
    ship[min(col, key=lambda p: p[1])] = "sea_lantern"

# ---------------------------------------------------------------- damage
for p, b in list(ship.items()):
    if b == "black_concrete" and rel(p)[0] < -3:
        n = noise(*p)
        if n > 0.2:
            ship[p] = "blackstone" if n < 0.7 else "coal_block"
    elif b in ("white_concrete", "light_gray_concrete"):
        a, _ = rel(p)
        if a + 2.5 * noise(*p) < -2.5:  # soot creeping up from the shield
            ship[p] = "blackstone" if noise(p[2], p[0], p[1]) > 0 else "gray_concrete"

# ---------------------------------------------------------------- terrain
X0, X1, Z0, Z1 = -45, 45, -45, 45
shield_w = R @ np.array([-0.5, 0, 0]) + offset
sh_xz = np.array([shield_w[0], shield_w[2]])
come_from = -axis_xz  # came in shield-first, skidded, then stopped


def base_h(x, z):
    return GROUND - 0.4 + 1.0 * math.sin(x * 0.12) * math.cos(z * 0.1) + 0.6 * math.sin((x - z) * 0.07)


terrain, surface = {}, {}
for x in range(X0, X1):
    for z in range(Z0, Z1):
        h = base_h(x, z)
        d = np.array([x + 0.5, z + 0.5]) - sh_xz
        r = float(np.linalg.norm(d))
        along = float(d @ come_from)
        across = float(d @ np.array([-come_from[1], come_from[0]]))
        top = "grass_block"
        if r < 6:
            h -= 2.0 * (1 - (r / 6) ** 2)
            top = "coarse_dirt"
        elif r < 8.5 and along < 1:
            h += 1.6 * (1 - abs(r - 7.2) / 1.3)  # soil shoved ahead of the shield
            top = "dirt"
        if 3 < along < 26 and abs(across) < 3.2 - along * 0.06:
            h -= 1.2 * (1 - along / 26)
            top = "coarse_dirt" if rng.random() < 0.7 else "gravel"
        hi = int(math.floor(h))
        surface[(x, z)] = hi
        for y in range(GROUND - 8, hi + 1):
            terrain[(x, y, z)] = top if y == hi else ("dirt" if y > hi - 3 else "stone")

# step up to the hatch: ground just outside sits one below the deck
if hatch_out:
    for depth in range(0, 3):
        for w in (0, 1):
            x = hatch_out[0] + hx * sgn * depth + px * w
            z = hatch_out[2] + hz * sgn * depth + pz * w
            for y in range(FLOOR, FLOOR + 4):
                terrain.pop((x, y, z), None)
            terrain[(x, FLOOR - 1, z)] = "coarse_dirt" if depth == 0 else "grass_block"
            surface[(x, z)] = FLOOR - 1

# the blown hatch cover, flat in the grass a few blocks out
if hatch_out:
    hc = np.array([hatch_out[0], hatch_out[2]], dtype=float) + side_xz * 6 + axis_xz * 2
    for i in range(-1, 2):
        for j in range(-1, 1):
            x = int(round(hc[0] + i * 0.95 + j * 0.3))
            z = int(round(hc[1] + j * 0.95 - i * 0.3))
            if (x, z) in surface:
                terrain[(x, surface[(x, z)] + 1, z)] = "iron_trapdoor_flat" if (i, j) != (0, 0) else "iron_block"

# parachute: a canopy dragged out flat behind the collar, alternating gores, a couple
# of folds standing up in wool, and its lines running back to the pod
nose_w = R @ np.array([11.5, 0, 0]) + offset
nz = np.array([nose_w[0], nose_w[2]])
CA, CC, CD = 6.5, 5.5, 14.0  # canopy half-length, half-width, centre distance from collar
for x in range(X0, X1):
    for z in range(Z0, Z1):
        d = np.array([x + 0.5, z + 0.5]) - nz
        a = float(d @ axis_xz) - CD
        c = float(d @ side_xz)
        e = (a / CA) ** 2 + (c / CC) ** 2
        if e > 1 + 0.12 * noise(x, 3, z):
            continue
        gore = int((math.degrees(math.atan2(c, a)) + 180) // 30)
        y = surface[(x, z)] + 1
        terrain[(x, y, z)] = "orange_carpet" if gore % 2 else "white_carpet"
        if 0.45 < e < 0.62 and noise(x, 9, z) > -0.2:  # a fold
            terrain[(x, y, z)] = "orange_wool" if gore % 2 else "white_wool"
for c in (-2.5, 0.0, 2.5):
    for t in np.linspace(1.0, CD - CA + 0.5, 30):
        q = nz + axis_xz * t + side_xz * c * (t / (CD - CA))
        x, z = int(math.floor(q[0])), int(math.floor(q[1]))
        if (x, z) in surface and (x, surface[(x, z)] + 1, z) not in terrain:
            terrain[(x, surface[(x, z)] + 1, z)] = "white_carpet"

# debris + small fires near the shield
for _ in range(14):
    d = come_from * rng.uniform(4, 22) + np.array([-come_from[1], come_from[0]]) * rng.normal(0, 3)
    x, z = int(math.floor(sh_xz[0] + d[0])), int(math.floor(sh_xz[1] + d[1]))
    if (x, z) in surface:
        terrain[(x, surface[(x, z)] + 1, z)] = rng.choice(["iron_block", "white_concrete", "blackstone", "fire"])
for p, b in list(ship.items()):
    up = (p[0], p[1] + 1, p[2])
    if b in ("coal_block", "blackstone") and up not in ship and up not in interior and rng.random() < 0.12:
        ship[up] = "fire"


def tree(x, z):
    y = surface[(x, z)] + 1
    for i in range(5):
        terrain[(x, y + i, z)] = "oak_log"
    for dx in range(-2, 3):
        for dz in range(-2, 3):
            for dy in range(3, 7):
                if abs(dx) + abs(dz) <= (3 if dy < 6 else 1) and not (dx == 0 and dz == 0 and dy < 5):
                    terrain.setdefault((x + dx, y + dy, z + dz), "oak_leaves")


for t in [(-14, 12), (16, -15), (-20, -9), (11, 19), (25, 6), (-6, -24)]:
    tree(*t)

for p in interior:
    terrain.pop(p, None)
for p in ship:
    terrain.pop(p, None)
for p in interior:  # deck-level clearance for terrain poking in
    terrain.pop((p[0], p[1] + 1, p[2]), None) if (p[0], p[1] + 1, p[2]) in interior else None

fig = None
if hatch_out:
    fx = hatch_out[0] + hx * sgn * 3 + px * 2
    fz = hatch_out[2] + hz * sgn * 3 + pz * 2
    fy = surface.get((fx, fz), FLOOR - 1) + 1
    fig = [[fx, fy, fz, "player_legs"], [fx, fy + 1, fz, "player_body"]]


# ---------------------------------------------------------------- render cameras (Blender coords)
def bl(v):
    return [float(v[0]), float(-v[2]), float(v[1])]


side3 = np.array([side_xz[0], 0, side_xz[1]])
ax3 = np.array([axis_xz[0], 0, axis_xz[1]])
mid = mid_w
shots = {
    "hatch_side": (bl(mid + side3 * 22 + ax3 * 6 + [0, 8, 0]), bl(mid + ax3 * 2), 30),
    "chute": (bl(mid + ax3 * 30 - side3 * 14 + [0, 14, 0]), bl(mid + ax3 * 8), 28),
    "shield": (bl(mid - ax3 * 24 - side3 * 10 + [0, 8, 0]), bl(mid), 30),
    "top": (bl(mid + ax3 * 6 + [0.1, 48, 0.1]), bl(mid + ax3 * 6), 30),
}
# inside: from the deck just in through the hatch, looking at the far wall
inner = [c for c in hatch_cells if c in interior and c[1] == FLOOR]
if inner:
    c = max(inner, key=lambda c: rel(c)[1])
    eye = np.array([c[0] + 0.5 + px * 0.5, FLOOR + 1.62, c[2] + 0.5 + pz * 0.5])
    shots["inside"] = (bl(eye), bl(eye - side3 * 4 + [0, -0.9, 0]), 11)
intact_c = bl(np.array([5.0, 64.0, 0.0]))
shots_intact = {
    "i_side": ([intact_c[0], intact_c[1] - 30, intact_c[2] + 2], intact_c, 35),
    "i_three": ([intact_c[0] + 18, intact_c[1] - 20, intact_c[2] + 13], intact_c, 35),
    "i_shield": ([intact_c[0] - 22, intact_c[1] - 10, intact_c[2] + 6], intact_c, 35),
    "i_nose": ([intact_c[0] + 24, intact_c[1] + 10, intact_c[2] + 8], intact_c, 35),
}
out = {
    "ship": [[*p, b] for p, b in ship.items()],
    "terrain": [[*p, b] for p, b in terrain.items()],
    "figure": fig or [],
    "intact": [[x, y + 64, z, b] for (x, y, z), b in intact.items()],
    "shots": shots,
    "shots_intact": shots_intact,
}
json.dump(out, open(sys.argv[1], "w"))
xs = np.array(list(ship))
print("pod blocks:", len(ship), "size:", xs.max(0) - xs.min(0) + 1, "floor cells:", len(floor_cells), "hatch_out:", hatch_out)
print(Counter(ship.values()).most_common())
