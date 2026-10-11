"""The mothership over spawn, Battlestar-style: a long slab-sided gunmetal carrier.
An armoured prow, a dorsal spine of twin turrets, a stepped command tower, a flight
pod on each flank with a lit hangar slot, and a blue-glowing engine cluster. Battle-damaged
but still flying, hanging over spawn. Axis-aligned (nose +x) so the slab sides stay crisp. Hollow shell.

python3 gen_battleship.py pod.json scene.json   (or ./build.sh)
"""

import json
import math
import sys
from collections import Counter

import numpy as np

rng = np.random.default_rng(5)
X0g, Y0g, Z0g = -12, -22, -34
NX, NY, NZ = 228, 66, 69
X, Y, Z = np.meshgrid(
    np.arange(NX) + X0g + 0.5, np.arange(NY) + Y0g + 0.5, np.arange(NZ) + Z0g + 0.5, indexing="ij"
)
AZ = np.abs(Z)


def cbox(x0, x1, y0, y1, zh, ch, zmin=0.0):
    """Box with chamfered long edges, optionally a slab offset from the centreline."""
    zz = AZ - zmin
    zc = zh - zmin
    yc = (y1 - y0) / 2
    ym = (y0 + y1) / 2
    return (
        (X >= x0) & (X <= x1) & (zz >= 0) & (zz <= zc) & (np.abs(Y - ym) <= yc)
        & ((np.abs(zz - zc / 2) - (zc / 2 - ch)) + (np.abs(Y - ym) - (yc - ch)) <= ch)
    )


solid = np.zeros(X.shape, bool)
solid |= cbox(0, 26, -14, 14, 19, 5)  # engine block
solid |= cbox(26, 42, -10, 11, 14, 4)  # aft neck
solid |= cbox(42, 158, -9, 10, 12, 3)  # spine
solid |= cbox(40, 160, 10, 13, 7, 2)  # dorsal ridge (turret deck)
solid |= cbox(30, 172, -12.5, -9, 7, 2)  # ventral keel
# flight pods: slabs off each flank, tapering at their forward ends
pods = cbox(56, 138, -6, 6, 25, 3, zmin=11.5) & ((X <= 126) | (AZ <= 25 - (X - 126) * 0.9))
solid |= pods
# prow: armoured head, top sloping down to a flat, bevelled nose
top = 16 - np.maximum(0, X - 156) * 0.16
prow = (X >= 156) & (X <= 204) & (Y >= -13) & (Y <= top) & (AZ <= 18 - np.maximum(0, X - 186) * 0.15)
prow &= (np.abs(AZ - 9) - 5) + (np.abs(Y - (top - 13) / 2) - ((top + 13) / 2 - 5)) <= 5
prow &= X <= 204 - np.maximum(0, Y - 2) * 0.8  # sloped glacis
prow &= X <= 204 - np.maximum(0, -Y - 6) * 0.9
solid |= prow
# engine nozzles on the stern
nozzles = [(0.0, 0.0, 6.5)] + [
    (10.8 * math.sin(a), 10.8 * math.cos(a), 3.6) for a in np.linspace(0, 2 * math.pi, 7)[:-1]
]
glow = np.zeros(X.shape, bool)
ring = np.zeros(X.shape, bool)
for zc, yc, r in nozzles:
    rr = np.hypot(Z - zc, Y - yc)
    solid |= (X >= -6) & (X < 1) & (rr <= r)
    glow |= (X >= -6) & (X < -4) & (rr <= r - 1.3)
    ring |= (X >= -6) & (X < -5) & (rr > r - 1.3) & (rr <= r)
# hangar slots in both pods, and the ventral launch bay the escape pod came from
hangar = (X >= 66) & (X <= 120) & (np.abs(Y) <= 3.5) & (AZ >= 14.5)
bay = (X >= 138) & (X <= 150) & (AZ <= 5) & (Y <= -6)
solid &= ~hangar & ~bay

pad = np.pad(solid, 1)
inner = (
    pad[2:, 1:-1, 1:-1] & pad[:-2, 1:-1, 1:-1] & pad[1:-1, 2:, 1:-1]
    & pad[1:-1, :-2, 1:-1] & pad[1:-1, 1:-1, 2:] & pad[1:-1, 1:-1, :-2]
)
shell = (solid & ~inner) | glow

PALETTE = ["polished_deepslate"] * 4 + ["deepslate_tiles"] * 3 + ["polished_blackstone"] * 2 + ["black_concrete"] * 2 + ["gray_concrete"]


def panel(x, y, z):
    h = (x // 9) * 73856093 ^ ((y + 50) // 5) * 19349663 ^ ((z + 50) // 7) * 83492791
    return PALETTE[h % len(PALETTE)]


blocks = {}
for i, j, k in np.argwhere(shell):
    x, y, z = int(i + X0g), int(j + Y0g), int(k + Z0g)
    if glow[i, j, k]:
        b = "sea_lantern"
    elif ring[i, j, k]:
        b = "light_blue_concrete"
    elif x < 0:
        b = "gray_concrete" if (y + z) % 4 else "black_concrete"
    else:
        b = panel(x, y, z)
        if x % 18 == 0:
            b = "black_concrete"  # section seams
        # lit ports: sparse warm windows in rows on the flanks
        flank = not (solid[i, j, k + 1] if k + 1 < NZ else False) or not (solid[i, j, k - 1] if k > 0 else False)
        if flank and y in (-5, -1, 3, 7) and x % 3 == 0 and rng.random() < 0.35:
            b = "shroomlight"
        # blue drive strips down the engine block
        if 2 <= x <= 24 and x % 6 == 3 and abs(abs(z) - 19) < 1.1 and abs(y) < 9:
            b = "sea_lantern"
    blocks[(x, y, z)] = b

# hangar interiors: lit ceilings, decks, hazard edge, a few parked fighters
for x in range(66, 121):
    for side in (1, -1):
        for az in range(14, 25):
            z = az * side
            blocks[(x, 4, z)] = "shroomlight" if (x % 3 == 0 and az % 3 == 1) else "gray_concrete"
            blocks[(x, -4, z)] = "polished_deepslate" if (x + az) % 6 else "gray_concrete"
            if az == 24:
                blocks[(x, -4, z)] = "orange_concrete" if (x // 2) % 2 else "black_concrete"
            if x in (66, 120):
                for y in range(-3, 4):
                    blocks[(x, y, z)] = "polished_deepslate"
        for y in range(-3, 4):
            blocks[(x, y, 14 * side)] = "polished_deepslate"  # back wall
for fx in (74, 86, 98, 110):  # little fighters, nose out
    for side in (1, -1):
        cz = 19 * side
        for dz, dx in [(0, 0), (0, 1), (0, 2), (side, 1), (-side, 1), (2 * side, 0), (-2 * side, 0)]:
            blocks[(fx + dx, -3, cz + dz)] = "light_gray_concrete" if dx != 2 else "black_concrete"
# ventral bay interior
for x in range(138, 151):
    for z in range(-5, 6):
        blocks[(x, -5, z)] = "shroomlight" if (x % 4 == 0 and z % 4 == 0) else "gray_concrete"

# command tower, stepped, with a lit bridge band
for x0, x1, y0, y1, zh in [(30, 62, 11, 15, 10), (34, 56, 15, 19, 8), (38, 52, 19, 23, 6), (40, 50, 23, 26, 7)]:
    for x in range(x0, x1 + 1):
        for z in range(-zh, zh + 1):
            for y in range(y0, y1 + 1):
                edge = x in (x0, x1) or abs(z) == zh or y == y1
                if not edge:
                    continue
                b = panel(x, y, z)
                if y0 == 23 and y == 24 and (x in (x0, x1) or abs(z) == zh):
                    b = "shroomlight" if (x + z) % 2 else "black_concrete"
                if y0 < 23 and y == y0 + 2 and (abs(z) == zh) and x % 2 == 0 and rng.random() < 0.6:
                    b = "shroomlight"
                blocks[(x, y, z)] = b
for ax, az, hgt in [(44, 0, 10), (47, 3, 6), (41, -4, 7), (36, 6, 4), (58, -6, 5)]:
    base = max(y for (x, y, z) in blocks if x == ax and z == az) if any(
        (ax, yy, az) in blocks for yy in range(0, 40)
    ) else 26
    for dy in range(1, hgt + 1):
        blocks[(ax, base + dy, az)] = "polished_basalt" if dy < hgt else "redstone_lamp"


def top_y(x, z):
    for y in range(45, -25, -1):
        if (x, y, z) in blocks:
            return y
    return None


TURRET_SPOTS = []  # (centre x, centre z, facing: +1 starboard / -1 port, deck y)


def turret(cx, cz, ybase, face):
    """Twin-gun turret trained out over one broadside (+z or -z)."""
    TURRET_SPOTS.append((cx, cz, face, ybase))

    def at(u, v, dy):  # u along the barrels (outboard), v along the ship
        return (cx + v, ybase + dy, cz + u * face)

    for u in range(-4, 4):
        for v in range(-3, 4):
            blocks[at(u, v, 1)] = "black_concrete"
            for dy in (2, 3, 4):
                if -3 <= u < 3 and abs(v) <= 3 - (dy == 4) and not (u == 2 and dy == 4):
                    blocks[at(u, v, dy)] = "gray_concrete" if dy == 4 else "polished_deepslate"
    for v in (-1, 1):
        for u in range(3, 12):
            blocks[at(u, v, 3)] = "polished_basalt"


# spine turrets alternate broadsides; pod turrets face out over their own side
for n, tx in enumerate((75, 97, 119, 141, 163, 181)):
    yb = top_y(tx, 0)
    if yb is not None:
        turret(tx, 0, yb, 1 if n % 2 == 0 else -1)
for tx in (65, 93, 121):
    for side in (1, -1):
        yb = top_y(tx, 19 * side)
        if yb is not None:
            turret(tx, 19 * side, yb, side)

# greebles: small modules and pipe runs over the flanks and deck
for _ in range(450):
    x = int(rng.uniform(4, 196))
    z = int(rng.choice([-1, 1]) * rng.uniform(0, 26))
    y0 = top_y(x, z)
    if y0 is None or rng.random() < 0.3 or abs(z) < 8:
        continue
    if any(abs(x - tx) < 8 and abs(z - tz) < 14 for tx, tz, _f, _y in TURRET_SPOTS):
        continue
    sx, sz, sy = int(rng.integers(2, 7)), int(rng.integers(1, 4)), int(rng.integers(1, 3))
    for dx in range(sx):
        for dz in range(sz):
            yy = top_y(x + dx, z + dz)
            if yy is None or abs(yy - y0) > 1:
                continue
            for dy in range(1, sy + 1):
                blocks.setdefault((x + dx, yy + dy, z + dz), "gray_concrete" if dy == sy else "black_concrete")
for y in (-8, 8):  # pipe runs along the spine flanks
    for side in (1, -1):
        for x in range(44, 150):
            if not (56 <= x <= 138):
                blocks.setdefault((x, y, (13) * side), "polished_basalt")

# navigation lights: red to port, green to starboard, at the prow, the pod tips and
# the stern. In-game a coloured glass pane over a sea lantern reads as a coloured lamp.
NAV = []


def nav_light(x, y, side):
    z_out = max((z for (bx, by, z) in blocks if bx == x and by == y and z * side > 0), key=lambda z: z * side, default=None)
    if z_out is None:
        return
    glass = "red_stained_glass" if side < 0 else "lime_stained_glass"
    for dy in (0, 1):
        for dx in (0, 1):
            blocks[(x + dx, y + dy, z_out)] = glass
            blocks[(x + dx, y + dy, z_out - side)] = "sea_lantern"
    NAV.append((x, y, z_out))


for side in (-1, 1):
    nav_light(194, 1, side)  # prow cheeks
    nav_light(133, 0, side)  # flight-pod tips
    nav_light(3, 9, side)  # stern quarters

# ---------------------------------------------------------------- interior rooms
# Built only in cells strictly inside the hull volume, so the outside is untouched
# (the bridge window band, below, is the one deliberate exterior change).
INNER = solid & inner


def is_inner(x, y, z):
    i, j, k = x - X0g, y - Y0g, z - Z0g
    return 0 <= i < NX and 0 <= j < NY and 0 <= k < NZ and bool(INNER[i, j, k])


def put(x, y, z, b, force=False):
    if force or is_inner(x, y, z):
        blocks[(x, y, z)] = b


def fill(x0, x1, y0, y1, z0, z1, b, force=False):
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            for z in range(z0, z1 + 1):
                put(x, y, z, b, force)


def clear(x0, x1, y0, y1, z0, z1, force=False):
    # only ever hollows cells inside the hull, so a room can't hole the outer plating
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            for z in range(z0, z1 + 1):
                if force or is_inner(x, y, z):
                    blocks.pop((x, y, z), None)


def walls(x0, x1, y0, y1, z0, z1, b):
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            for z in range(z0, z1 + 1):
                if x in (x0, x1) or z in (z0, z1):
                    put(x, y, z, b)


DECKS = (-8, -2, 4)  # floor levels: hold, main deck, upper deck
INTERIOR_SHOTS = {}

# main deck plating the length of the spine, and the hold below it
fill(28, 156, -2, -2, -11, 11, "polished_deepslate")
fill(44, 150, -8, -8, -11, 11, "gray_concrete")
fill(30, 156, 4, 4, -11, 11, "polished_deepslate")

# main corridor, deck 2
walls(28, 156, -1, 2, -3, 3, "gray_concrete")
fill(29, 155, 3, 3, -3, 3, "black_concrete")
clear(29, 155, -1, 2, -2, 2)
for x in range(30, 156, 4):
    put(x, 3, 0, "sea_lantern")
    put(x, -1, -3, "light_blue_concrete")  # floor-level guide strip
    put(x, -1, 3, "light_blue_concrete")
INTERIOR_SHOTS["in_corridor"] = ((60, 0.62, 0.5), (100, -0.3, 0), 20)


def door(x, z):
    clear(x, x, -1, 0, z, z)


# crew cabins either side, x 66..96
for x0 in range(66, 96, 6):
    for side in (1, -1):
        z0, z1 = (4, 11) if side > 0 else (-11, -4)
        walls(x0, x0 + 6, -1, 2, min(z0 - 1, z1), max(z0, z1 + 1), "light_gray_concrete")
        fill(x0, x0 + 6, -1, 2, 3 * side, 3 * side, "light_gray_concrete")
        clear(x0 + 1, x0 + 5, -1, 2, min(z0, z1), max(z0, z1))
        door(x0 + 3, 3 * side)
        zb = 10 * side
        put(x0 + 1, -1, zb, "bed_head")
        put(x0 + 2, -1, zb, "bed_feet")
        put(x0 + 5, -1, zb, "barrel")
        put(x0 + 5, -1, zb - side, "crafting_table" if x0 % 12 == 0 else "barrel")
        put(x0 + 3, 2, 8 * side, "shroomlight")
INTERIOR_SHOTS["in_cabin"] = ((71.2, 0.62, 4.2), (67.5, -1.0, 10.5), 14)

# mess hall, port, x 100..128
walls(100, 128, -1, 2, -12, -3, "polished_blackstone")
clear(101, 127, -1, 2, -11, -4)
for x in (104, 110, 116, 122):
    door(x, -3)
for x in range(103, 126):
    for z in (-6, -9):
        if x % 8 not in (0, 7):
            put(x, -1, z, "oak_planks")  # long tables
for x in range(102, 126, 4):
    put(x, 2, -7, "shroomlight")
    put(x, 2, -10, "shroomlight")
for x in range(101, 108):  # galley counter
    put(x, -1, -11, "furnace" if x % 2 else "smooth_stone")
    put(x, 0, -11, "barrel" if x % 3 == 0 else "smooth_stone")
INTERIOR_SHOTS["in_mess"] = ((126.5, 0.62, -4.5), (103, -0.8, -9.5), 16)

# medbay, starboard, x 100..118: white, cyan, beds in a row
walls(100, 118, -1, 2, 3, 12, "white_concrete")
clear(101, 117, -1, 2, 4, 11)
fill(101, 117, -2, -2, 4, 11, "white_concrete")
door(104, 3)
door(105, 3)
for x in range(102, 117, 3):
    put(x, -1, 11, "bed_head")
    put(x, -1, 10, "bed_feet")
    put(x, 0, 11, "cyan_concrete")
    put(x, 2, 9, "sea_lantern")
for x in range(108, 113):
    put(x, -1, 5, "cyan_concrete")
INTERIOR_SHOTS["in_medbay"] = ((101.5, 0.62, 4.5), (115, -0.7, 10), 16)

# armoury, starboard, x 120..140: racks and crates
walls(120, 140, -1, 2, 3, 12, "polished_deepslate")
clear(121, 139, -1, 2, 4, 11)
door(124, 3)
for x in range(122, 139, 2):
    put(x, -1, 11, "iron_block")
    put(x, 0, 11, "polished_basalt")
    put(x, 1, 11, "iron_block")
for x in range(126, 136, 3):
    for z in (6, 7):
        put(x, -1, z, "barrel")
        put(x, 0, z, "barrel" if (x + z) % 2 else "iron_block")
for x in range(122, 140, 5):
    put(x, 2, 7, "shroomlight")
INTERIOR_SHOTS["in_armoury"] = ((122, 0.62, 4.5), (137, -0.5, 9), 16)

# cargo hold, deck 3, full width: crate stacks, pillars, lights; open to the belly breach
for x in range(46, 150, 8):
    for z in (-6, 6):
        fill(x, x, -7, -3, z, z, "polished_basalt")
for x in range(48, 148):
    for z in range(-10, 11):
        if (x // 4 + z // 4) % 3 == 0 and abs(z) > 2 and x % 4 != 3 and z % 4 != 3:
            h = 1 + (x * 7 + z * 3) % 3
            for y in range(-7, -7 + h):
                put(x, y, z, "barrel" if (x + y + z) % 3 else "oak_planks")
for x in range(48, 148, 6):
    for z in (-9, 0, 9):
        put(x, -3, z, "sea_lantern")
INTERIOR_SHOTS["in_hold"] = ((60, -5.4, 0.5), (100, -6.2, -2), 16)

# engine room: the whole engine block, a reactor column, ring catwalks, conduits
for x in range(1, 26):
    for z in range(-18, 19):
        put(x, -13, z, "polished_deepslate")
RX, RZ = 13, 0
for y in range(-12, 13):
    for x in range(RX - 4, RX + 5):
        for z in range(RZ - 4, RZ + 5):
            r = math.hypot(x - RX, z - RZ)
            if r <= 1.6:
                put(x, y, z, "sea_lantern")
            elif r <= 3.3:
                put(x, y, z, "iron_block" if y % 6 == 0 else "light_blue_stained_glass")
for yd in DECKS:
    for x in range(1, 26):
        for z in range(-17, 18):
            r = math.hypot(x - RX, z - RZ)
            if 4.5 <= r <= 9.5 or (abs(z) <= 1 and r > 3.4):
                put(x, yd, z, "polished_deepslate" if (x + z) % 4 else "gray_concrete")
for zc, yc, _r in nozzles:  # conduits from the core out to each nozzle
    for x in range(1, RX - 3):
        put(x, int(round(yc)), int(round(zc)), "polished_basalt")
for y in (-10, -4, 2, 8):
    for z in range(-16, 17, 4):
        put(1, y, z, "sea_lantern")
        put(25, y, z, "sea_lantern")
# the corridor reaches the engine room through the aft bulkhead
clear(24, 28, -1, 2, -2, 2)
INTERIOR_SHOTS["in_engine"] = ((22.5, -0.38, 11.5), (13, 2.5, 0), 13)

# bridge: open the top tower tier into the bridge for one tall room, glass band all round
clear(41, 49, 23, 23, -5, 5, force=True)
for x in range(40, 51):
    for z in range(-7, 8):
        if (x in (40, 50) or abs(z) == 7) and (x, 24, z) in blocks:
            blocks[(x, 24, z)] = "glass"
            blocks[(x, 25, z)] = "glass"
for x in range(39, 52):
    for z in range(-5, 6):
        put(x, 19, z, "polished_deepslate", force=True)
for z in range(-5, 6):  # forward console arc
    put(48, 20, z, "black_concrete", force=True)
    put(48, 21, z, "redstone_lamp" if z % 2 else "light_blue_stained_glass", force=True)
for x in range(42, 48):
    for z in (-5, 5):
        put(x, 20, z, "black_concrete", force=True)
        put(x, 21, z, "light_blue_stained_glass" if x % 2 else "redstone_lamp", force=True)
put(42, 20, 0, "polished_blackstone", force=True)  # command chair on a dais
put(42, 21, 0, "polished_blackstone", force=True)
fill(41, 43, 20, 20, -1, 1, "gray_concrete", force=True)
put(42, 21, 0, "black_concrete", force=True)
put(45, 20, 0, "sea_lantern", force=True)  # holo table
put(45, 21, 0, "light_blue_stained_glass", force=True)
for x in (42, 46):
    for z in (-3, 3):
        put(x, 25, z, "sea_lantern", force=True)
INTERIOR_SHOTS["in_bridge"] = ((40.8, 21.9, -4.2), (49, 20.8, 1.5), 14)

# combat information centre, the tier below the bridge
for x in range(35, 56):
    for z in range(-7, 8):
        put(x, 15, z, "black_concrete", force=True)
fill(42, 48, 16, 16, -2, 2, "sea_lantern", force=True)  # plot table
fill(42, 48, 17, 17, -2, 2, "light_blue_stained_glass", force=True)
for x in range(36, 55):
    for z in (-7, 7):
        put(x, 16, z if (x, 16, z) not in blocks else z - (1 if z > 0 else -1), "black_concrete", force=True)
for x in range(37, 54, 4):
    put(x, 18, -4, "sea_lantern", force=True)
    put(x, 18, 4, "sea_lantern", force=True)
INTERIOR_SHOTS["in_cic"] = ((36, 17.4, 5.5), (48, 16, -1), 14)

# corridor wall lights last, so the room walls built along it don't cover them
for x in range(30, 156, 8):
    for z in (-3, 3):
        if (x, 0, z) in blocks:
            blocks[(x, 2, z)] = "shroomlight"


# ---------------------------------------------------------------- more interior: access
def ladder(x, z, y0, y1, wall_dx, wall_dz, holes=()):
    """A ladder climbing y0..y1 at (x, z), hung on the wall block at +(wall_dx, wall_dz)."""
    face = {(1, 0): "e", (-1, 0): "w", (0, 1): "s", (0, -1): "n"}[(wall_dx, wall_dz)]
    for y in range(y0, y1 + 1):
        blocks[(x + wall_dx, y, z + wall_dz)] = blocks.get((x + wall_dx, y, z + wall_dz), "gray_concrete") \
            if (x + wall_dx, y, z + wall_dz) in blocks else "gray_concrete"
        blocks[(x, y, z)] = "ladder_" + face
    for y in holes:  # the decks it passes through
        blocks[(x, y, z)] = "ladder_" + face


# shaft 1: hold -> main deck -> upper deck, beside the corridor at x 57
clear(54, 60, -7, -3, 2, 8)  # keep the crate stacks off the foot of the ladder
ladder(57, 5, -7, 8, 0, 1, holes=(-2, 4))
blocks.pop((57, -1, 3), None)  # door from the corridor
blocks.pop((57, 0, 3), None)
# shaft 2: upper deck -> up through the neck and tower tier -> combat centre
for y in range(5, 16):
    blocks.pop((37, y, -5), None)
ladder(37, -5, 5, 15, 0, -1, holes=(11, 15))
put(37, 16, -6, "gray_concrete", force=True)
# shaft 3: combat centre -> bridge
for y in range(16, 20):
    blocks.pop((40, y, -4), None)
ladder(40, -4, 16, 19, 0, -1, holes=(19,))
# engine room: a ladder up the reactor's flank linking the catwalks
ladder(13, -4, -12, 5, 0, 1)
INTERIOR_SHOTS["in_shaft"] = ((57.5, 0.4, 0.2), (57.5, 0.6, 5.5), 14)

# ---------------------------------------------------------------- upper deck (floor y 4)
walls(30, 156, 5, 8, -3, 3, "gray_concrete")
fill(31, 155, 9, 9, -3, 3, "black_concrete")
clear(31, 155, 5, 8, -2, 2)
for x in range(32, 156, 6):
    put(x, 9, 0, "sea_lantern")


def door_up(x, z):
    clear(x, x, 5, 6, z, z)


# hydroponics garden, port x 60..100: farmland rows of wheat and carrots under grow lights
walls(60, 100, 5, 8, -12, -3, "polished_deepslate")
clear(61, 99, 5, 8, -11, -4)
for x in (66, 80, 94):
    door_up(x, -3)
for x in range(61, 100):
    for z in range(-11, -3):
        if z in (-7,):
            put(x, 4, z, "polished_deepslate")  # walkway
            continue
        put(x, 4, z, "farmland")
        put(x, 5, z, "wheat" if (x // 6) % 2 else "carrots")
    if x % 4 == 1:
        put(x, 8, -9, "shroomlight")
        put(x, 8, -5, "shroomlight")
for x in range(62, 100, 9):
    put(x, 4, -11, "moss_block")  # farmland under a bush would turn to dirt
    put(x, 5, -11, "oak_leaves")
    put(x, 6, -11, "oak_leaves")
INTERIOR_SHOTS["in_garden"] = ((61.5, 6.62, -7.0), (99, 5.2, -7.5), 16)

# captain's quarters, starboard x 60..78: bed, bookshelves, red carpet, a desk
walls(60, 78, 5, 8, 3, 12, "polished_blackstone")
clear(61, 77, 5, 8, 4, 11)
door_up(66, 3)
for x in range(62, 77):
    for z in range(5, 11):
        put(x, 5, z, "red_carpet")
for x in range(61, 78):
    put(x, 5, 11, "bookshelf")
    put(x, 6, 11, "bookshelf")
put(75, 5, 7, "bed_head")
put(74, 5, 7, "bed_feet")
for z in (5, 6, 7):
    put(66, 5, z + 3, "oak_planks")  # desk
put(66, 6, 9, "lantern")
for x in (64, 72):
    put(x, 8, 7, "shroomlight")
INTERIOR_SHOTS["in_captain"] = ((61.5, 6.62, 4.5), (75, 5.4, 9.5), 16)

# briefing room, starboard x 80..110: benches facing a big screen wall
walls(80, 110, 5, 8, 3, 12, "gray_concrete")
clear(81, 109, 5, 8, 4, 11)
door_up(84, 3)
for z in range(4, 12):
    for y in range(5, 9):
        put(109, y, z, "light_blue_stained_glass" if 5 < z < 11 and 5 < y < 8 else "black_concrete")
    put(108, 6, z, "sea_lantern") if 5 < z < 11 else None
for x in range(84, 104, 3):
    for z in range(5, 11):
        if z != 8:
            put(x, 5, z, "oak_planks")
for x in range(84, 108, 6):
    put(x, 8, 8, "shroomlight")
INTERIOR_SHOTS["in_briefing"] = ((81.5, 6.9, 7.5), (109, 6.0, 8), 16)

# workshop, port x 104..140: benches, anvils, furnaces, smithing tables
walls(104, 140, 5, 8, -12, -3, "polished_deepslate")
clear(105, 139, 5, 8, -11, -4)
door_up(110, -3)
door_up(130, -3)
for x in range(105, 140):
    put(x, 5, -11, ["crafting_table", "furnace", "smithing_table", "anvil", "barrel"][x % 5])
for x in range(108, 138, 6):
    for z in (-7, -8):
        put(x, 5, z, "iron_block")
        put(x + 1, 5, z, "iron_block")
    put(x, 6, -7, "anvil")
for x in range(106, 140, 5):
    put(x, 8, -7, "sea_lantern")
INTERIOR_SHOTS["in_workshop"] = ((106, 6.62, -4.5), (136, 5.2, -9.5), 16)

# ---------------------------------------------------------------- prow
# fire control on the main deck at the corridor's forward end
clear(156, 156, -1, 2, -2, 2)
walls(157, 176, -1, 3, -10, 10, "polished_blackstone")
clear(158, 175, -1, 3, -9, 9)
fill(158, 175, -2, -2, -9, 9, "polished_deepslate")
fill(158, 175, 4, 4, -9, 9, "black_concrete")
for z in range(-8, 9):
    put(175, -1, z, "black_concrete")
    put(175, 0, z, "redstone_lamp" if z % 3 else "light_blue_stained_glass")
    for y in (1, 2, 3):
        put(175, y, z, "light_blue_stained_glass" if abs(z) < 7 and y < 3 else "black_concrete")
for x in range(160, 172, 4):
    for z in (-6, 6):
        put(x, -1, z, "black_concrete")
        put(x, 0, z, "redstone_lamp")
    put(x, 3, 0, "sea_lantern")
put(166, -1, 0, "sea_lantern")
put(166, 0, 0, "light_blue_stained_glass")
INTERIOR_SHOTS["in_firecontrol"] = ((158.5, 0.62, -0.5), (175, 0.3, 0), 16)

# magazine below: TNT racks between iron frames, reached by a ladder from fire control
walls(157, 186, -7, -3, -12, 12, "iron_block")
clear(158, 185, -7, -3, -11, 11)
fill(158, 185, -8, -8, -11, 11, "polished_deepslate")
for x in range(160, 185, 3):
    for z in list(range(-10, -3)) + list(range(4, 11)):
        for y in (-7, -6):
            put(x, y, z, "tnt")
        put(x, -5, z, "iron_block")
for x in range(159, 186, 5):
    put(x, -3, 0, "redstone_lamp")
blocks.pop((160, -2, 0), None)
ladder(160, 0, -7, -1, 1, 0)
INTERIOR_SHOTS["in_magazine"] = ((158.6, -5.4, 0.5), (184, -6.3, 4), 16)

# ---------------------------------------------------------------- flight pods
# cross-passages from the main deck out to each pod, at x 58..64
for side in (1, -1):
    clear(58, 64, -1, 2, 3 * side, 3 * side)  # opening in the corridor wall
    zs = sorted((4 * side, 13 * side))
    walls(57, 65, -1, 3, zs[0] - (1 if side < 0 else 0), zs[1] + (1 if side > 0 else 0), "gray_concrete")
    clear(58, 64, -1, 2, zs[0], zs[1])
    fill(58, 64, -2, -2, zs[0], zs[1], "polished_deepslate")
    # hangar control booth at the hangar's aft end, raised to look over the deck
    lo, hi = sorted((14 * side, 24 * side))
    clear(58, 65, -1, 2, lo, hi)
    fill(58, 65, -2, -2, lo, hi, "polished_deepslate")
    fill(58, 65, 3, 3, lo, hi, "black_concrete")
    for z in range(lo, hi + 1):
        for y in (-1, 0, 1, 2):
            # window over the hangar: the hangar's end wall and the plating behind it
            # (which faces only into the hangar, never outside)
            blocks[(66, y, z)] = "glass"
            if (65, y, z) in blocks:
                blocks[(65, y, z)] = "glass"
        if z % 3 == 0:
            put(64, -1, z, "black_concrete")
            put(64, 0, z, "redstone_lamp")
    put(61, 3, 19 * side, "sea_lantern")
    put(61, 3, 16 * side, "sea_lantern")
INTERIOR_SHOTS["in_hangar_ctl"] = ((59.0, 1.2, 15.2), (80, -3.5, 21.0), 15)

# pilots' ready room, port pod forward of the hangar (x 122..137), off a passage at x 130
clear(130, 131, -1, 0, -3, -3)
for x in (129, 132):
    fill(x, x, -1, 2, -15, -4, "gray_concrete")
clear(130, 131, -1, 2, -15, -4)
fill(130, 131, -2, -2, -15, -4, "polished_deepslate")
walls(121, 137, -1, 2, -24, -15, "polished_blackstone")
clear(122, 136, -1, 2, -23, -16)
clear(130, 131, -1, 0, -15, -15)
fill(122, 136, -2, -2, -23, -16, "polished_deepslate")
for x in range(122, 137):
    put(x, -1, -23, "barrel")  # lockers
    put(x, 0, -23, "barrel" if x % 2 else "iron_block")
for x in range(124, 135, 2):
    put(x, -1, -19, "oak_planks")  # benches
for x in range(123, 137, 4):
    put(x, 2, -19, "shroomlight")
INTERIOR_SHOTS["in_readyroom"] = ((123, 0.62, -16.5), (135, -0.5, -22), 16)

# maintenance bay, starboard pod forward of the hangar (x 122..137), off the armoury;
# the pod breach blows its outer wall open
clear(130, 130, -1, 0, 12, 12)
walls(121, 137, -1, 3, 13, 24, "gray_concrete")
clear(122, 136, -1, 3, 14, 23)
fill(122, 136, -2, -2, 14, 23, "gray_concrete")
for x in range(123, 136, 3):
    put(x, -1, 15, "iron_block")
    put(x, 0, 15, "anvil")
    put(x, -1, 22, "smithing_table" if x % 2 else "crafting_table")
for x in (125, 129, 133):
    fill(x, x + 1, -1, -1, 18, 19, "light_gray_concrete")  # a fighter frame on stands
    put(x, 0, 18, "black_concrete")
for x in range(123, 137, 4):
    put(x, 3, 18, "sea_lantern")
INTERIOR_SHOTS["in_maintenance"] = ((122.5, 2.4, 20.5), (135, -1.0, 17.5), 14)

_holes = [tuple(int(v) for v in (i + X0g, j + Y0g, k + Z0g)) for i, j, k in np.argwhere(shell)]
print("shell cells missing after interiors:", sum(1 for p in _holes if p not in blocks))
# ---------------------------------------------------------------- battle damage
# Badly hurt but still flying: five breaches opening onto lit decks, a turret with its
# guns shot away, scorch marks, debris drifting off the wounds. Every engine stays lit.
_w = [(rng.normal(0, 1, 3) * f, rng.uniform(0, 6.3)) for f in (0.2, 0.4, 0.8) for _ in range(3)]


def noise(x, y, z):
    return sum(math.sin(k[0] * x + k[1] * y + k[2] * z + ph) for k, ph in _w) / 3.0


def was_solid(x, y, z):
    i, j, k = x - X0g, y - Y0g, z - Z0g
    return 0 <= i < NX and 0 <= j < NY and 0 <= k < NZ and bool(solid[i, j, k])


BREACHES = [  # centre (local), radius: holes punched through the plating
    ((50, 3, -14), 6.5),  # port flank, aft of the tower
    ((176, 6, 17), 7.3),  # starboard prow
    ((128, 1, 25), 6.0),  # starboard flight pod, forward of the hangar
    ((106, 11, 4), 5.2),  # dorsal, through a turret
    ((92, -12, -3), 5.5),  # belly
]
SCORCHES = [  # blast marks only, no hole
    ((150, -12, 6), 4.0),
    ((18, 9, 19), 3.5),
]
for (bx, by, bz), r in SCORCHES:
    for x in range(bx - 9, bx + 10):
        for y in range(by - 9, by + 10):
            for z in range(bz - 9, bz + 10):
                p = (x, y, z)
                if p not in blocks or blocks[p] in ("sea_lantern", "light_blue_concrete"):
                    continue
                heat = (r + 4 - math.dist(p, (bx, by, bz))) / 3.0 + 0.45 * noise(y, z, x)
                if heat > 0.6:
                    blocks[p] = "coal_block" if heat > 1.3 else "blackstone"
for (bx, by, bz), r in BREACHES:
    span = int(r + 6)
    for x in range(bx - span, bx + span + 1):
        for y in range(by - span, by + span + 1):
            for z in range(bz - span, bz + span + 1):
                d = math.dist((x, y, z), (bx, by, bz)) + 2.2 * noise(x, y, z)
                p = (x, y, z)
                if d < r:
                    blocks.pop(p, None)
                    if was_solid(x, y, z):
                        # the inside the wound shows: decks, bulkheads, ribs, embers
                        if y in DECKS and d < r - 0.8:
                            blocks[p] = "polished_deepslate" if noise(z, x, y) > -0.5 else "gray_concrete"
                        elif y in (-3, 3, 9) and x % 5 == 0 and abs(z) % 3 == 0 and d < r - 1.5:
                            # deck lights, a few gone dark
                            blocks[p] = "shroomlight" if rng.random() < 0.7 else "redstone_lamp"
                        elif x % 12 == 0 and d < r - 2.5:
                            blocks[p] = "gray_concrete"
                        elif x % 4 == 0 and r - 1.6 < d < r:
                            blocks[p] = "polished_basalt"  # exposed frame ribs
                        elif d < r * 0.45 and rng.random() < 0.05:
                            blocks[p] = "magma"
                elif d < r + 4.5 and p in blocks and blocks[p] not in ("sea_lantern", "light_blue_concrete"):
                    heat = (r + 4.5 - d) / 3.0 + 0.4 * noise(y, z, x)
                    if blocks[p] == "shroomlight" or heat > 0.6:
                        blocks[p] = "coal_block" if heat > 1.25 else "blackstone"
                    if heat > 1.4 and rng.random() < 0.06:
                        blocks[p] = "magma"
# fires on scorched upward faces near the wounds, and signal campfires (a hay bale
# under a campfire sends its smoke up ~24 blocks instead of ~8) to make them smoke
SMOKE = []
for p, b in list(blocks.items()):
    up = (p[0], p[1] + 1, p[2])
    if b in ("coal_block", "magma", "blackstone") and up not in blocks:
        roll = rng.random()
        if roll < 0.045 and all(math.dist(p, q) > 5 for q in SMOKE):
            blocks[up] = "campfire"
            hidden = all((p[0] + a, p[1], p[2] + c) in blocks for a, c in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            if hidden:
                blocks[p] = "hay_block"  # boxed in by plating, so it doesn't show
            SMOKE.append(p)
        elif b != "blackstone" and roll < 0.2:
            blocks[up] = "fire"
# a turret with its guns shot away: barrels stop short, one bent down
tx, tz, tf, ty = TURRET_SPOTS[3]
for u in range(6, 12):
    for v in (-1, 1):
        blocks.pop((tx + v, ty + 3, tz + u * tf), None)
for k in range(4):
    blocks[(tx + 1, ty + 3 - k, tz + (6 + k // 2) * tf)] = "polished_basalt"
# debris drifting off the breaches, a few chunks each
DEBRIS = ["polished_deepslate", "black_concrete", "deepslate_tiles", "gray_concrete", "blackstone", "polished_basalt"]
for (bx, by, bz), r in BREACHES:
    out = np.array([0.0, 0.0, 0.0])
    out[2] = math.copysign(1, bz) if abs(bz) > 10 else 0
    out[1] = 1 if by > 9 else (-1 if by < -9 else 0)
    if not out.any():
        out[2] = 1
    for _ in range(int(r * 1.2)):
        c = np.array([bx, by, bz]) + out * rng.uniform(r * 0.6, r * 3.2) + rng.normal(0, r * 0.5, 3)
        for _ in range(int(rng.integers(1, 4))):
            q = tuple(int(v) for v in c + rng.integers(-1, 2, 3))
            if not was_solid(*q):
                blocks[q] = str(rng.choice(DEBRIS))

# ---------------------------------------------------------------- place over spawn
pod = json.load(open(sys.argv[1]))
ALT = 192
SX, SZ = -100, -62  # stern sits here; nose points east
world = [[x + SX, y + ALT, z + SZ, b] for (x, y, z), b in blocks.items()]
xs = np.array([w[:3] for w in world])
print("mothership blocks:", len(world), "size:", xs.max(0) - xs.min(0) + 1, "bbox:", xs.min(0), xs.max(0))
print(Counter(b for *_, b in world).most_common(10))


def bl(v):
    return [float(v[0]), float(-v[2]), float(v[1])]


def wpt(x, y, z):
    return np.array([x + SX, y + ALT, z + SZ], dtype=float)


ship_mid = wpt(100, 0, 0)
pod_mid = np.array([5.0, 66.0, 1.0])
away = pod_mid - np.array([ship_mid[0], pod_mid[1], ship_mid[2]])
away /= np.linalg.norm(away)
side = np.array([-away[2], 0, away[0]])
pod["ship"] += world
pod["shots"] = {
    "from_ground": (bl(pod_mid + away * 22 + side * 9 + [0, 1.8, 0]), bl(pod_mid * 0.68 + ship_mid * 0.32), 10),
    "pod_up": (bl(pod_mid + away * 13 - side * 7 + [0, 1.0, 0]), bl(pod_mid * 0.86 + ship_mid * 0.14), 11),
    "far": (bl(pod_mid + away * 190 + side * 60 + [0, 16, 0]), bl(pod_mid * 0.45 + ship_mid * 0.55), 17),
    "ship_quarter": (bl(wpt(250, 70, 190)), bl(wpt(100, 0, 0)), 24),
    "ship_stern": (bl(wpt(-110, 40, 110)), bl(wpt(60, 0, 0)), 24),
    "hangar": (bl(wpt(95, 6, 75)), bl(wpt(92, 0, 18)), 26),
    "prow_damage": (bl(wpt(205, 16, 55)), bl(wpt(174, 4, 16)), 30),
    "port_damage": (bl(wpt(62, 12, -50)), bl(wpt(50, 3, -14)), 30),
    "ship_side": (bl(wpt(100, 10, 260)), bl(wpt(100, 0, 0)), 30),
}
pod["smoke"] = [[p[0] + SX + 0.5, p[1] + ALT + 1.5, p[2] + SZ + 0.5, 1 if blocks.get(p) == "hay_block" else 0] for p in SMOKE]
for name, (eye, tgt, lens) in INTERIOR_SHOTS.items():
    pod["shots"][name] = (bl(wpt(*eye)), bl(wpt(*tgt)), lens)
print("campfires:", len(SMOKE), "signal:", sum(1 for p in SMOKE if blocks.get(p) == "hay_block"))
json.dump(pod, open(sys.argv[2], "w"))
