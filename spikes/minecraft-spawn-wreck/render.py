"""blender -b -P render.py -- voxels.json tex_dir out_dir [ship_only]

Builds one culled block mesh with vanilla textures and renders preview views."""

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1 :]
data = json.load(open(argv[0]))
TEX = Path(argv[1])
OUT = Path(argv[2])
SHIP_ONLY = len(argv) > 3 and argv[3] == "ship_only"
OUT.mkdir(exist_ok=True)

# block -> (top, bottom, side) texture
FACES = {
    "white_concrete": "concrete_white",
    "light_gray_concrete": "concrete_silver",
    "gray_concrete": "concrete_gray",
    "black_concrete": "concrete_black",
    "orange_concrete": "concrete_orange",
    "iron_block": "iron_block",
    "polished_deepslate": "polished_deepslate",
    "blackstone": ("blackstone_top", "blackstone_top", "blackstone"),
    "coal_block": "coal_block",
    "light_blue_stained_glass": "glass_light_blue",
    "sea_lantern": "sea_lantern",
    "redstone_lamp": "redstone_lamp_on",
    "magma": "magma",
    "smooth_stone": "stone_slab_top",
    "barrel": ("barrel_top", "barrel_bottom", "barrel_side"),
    "grass_block": ("grass_top", "dirt", "grass_side"),
    "dirt": "dirt",
    "coarse_dirt": "coarse_dirt",
    "gravel": "gravel",
    "stone": "stone",
    "oak_log": ("log_oak_top", "log_oak_top", "log_oak"),
    "oak_leaves": "leaves_oak",
    "crafting_table": ("crafting_table_top", "planks_oak", "crafting_table_side"),
    "furnace": ("furnace_top", "furnace_top", "furnace_side"),
    "bed_head": ("bed_head_top", "planks_oak", "bed_head_side"),
    "bed_feet": ("bed_feet_top", "planks_oak", "bed_head_side"),
    "deepslate_tiles": "deepslate_tiles",
    "deepslate_bricks": "deepslate_bricks",
    "polished_blackstone": "polished_blackstone",
    "shroomlight": "shroomlight",
    "glowstone": "glowstone",
    "polished_basalt": ("polished_basalt_top", "polished_basalt_top", "polished_basalt_side"),
    "light_blue_concrete": "concrete_light_blue",
    "cyan_concrete": "concrete_cyan",
    "glass": "glass",
    "red_stained_glass": "glass_red",
    "lime_stained_glass": "glass_lime",
    "oak_planks": "planks_oak",
    "campfire": "campfire_log_lit",
    "hay_block": ("hay_top", "hay_top", "hay_side"),
    "bookshelf": ("planks_oak", "planks_oak", "bookshelf"),
    "tnt": ("tnt_top", "tnt_top", "tnt_side"),
    "farmland": ("farmland", "dirt", "dirt"),
    "red_carpet": "wool_red",
    "smithing_table": ("smithing_top", "smithing_top", "smithing_side"),
    "anvil": ("anvil_top", "anvil_base", "anvil_base"),
    "lantern": "lantern",
    "white_carpet": "wool_white",
    "sandstone": ("sandstone_top", "sandstone_top", "sandstone_side"),
    "jungle_log": ("log_jungle_top", "log_jungle_top", "log_jungle"),
    "jungle_leaves": "leaves_jungle",
    "mangrove_leaves": "leaves_jungle",
    "mangrove_log": ("log_jungle_top", "log_jungle_top", "log_jungle"),
    "spruce_log": ("log_spruce_top", "log_spruce_top", "log_spruce"),
    "spruce_leaves": "leaves_spruce",
    "andesite": "andesite",
    "cobblestone": "cobblestone",
    "ochre_froglight": ("ochre_top", "ochre_top", "ochre_side"),
    "snow_layer": "snow",
    "water": "water",
    "moss_block": "moss",
    "sand": "sand",
    "red_sand": "red_sand",
    "mud": "mud",
    "packed_mud": "packed_mud",
    "orange_terracotta": "terracotta_orange",
    "red_sandstone": ("red_sandstone_top", "red_sandstone_top", "red_sandstone_side"),
    "bed": ("bed_head_top", "planks_oak", "bed_head_side"),
    "iron_trapdoor": "iron_trapdoor",
    "white_wool": "wool_white",
    "orange_wool": "wool_orange",
    "orange_carpet": "wool_orange",
    "iron_trapdoor_flat": "iron_trapdoor",
}
# blocks the game draws thinner than a cube: height of the box
THIN = {"snow_layer": 2 / 16, "iron_trapdoor": 3 / 16, "bed": 9 / 16, "red_carpet": 1 / 16, "farmland": 15 / 16, "anvil": 1.0, "lantern": 9 / 16, "campfire": 7 / 16, "white_carpet": 1 / 16, "orange_carpet": 1 / 16, "iron_trapdoor_flat": 3 / 16, "bed_head": 9 / 16, "bed_feet": 9 / 16}
EMIT = {"ochre_top": 3.0, "ochre_side": 3.0, "glass_red": 6.0, "glass_lime": 6.0, "campfire_log_lit": 2.0, "shroomlight": 8.0, "glowstone": 3.0, "sea_lantern": 3.0, "redstone_lamp_on": 3.0, "magma": 2.5, "fire_0": 6.0}
TRANSLUCENT = {"water", "light_blue_stained_glass", "glass", "red_stained_glass", "lime_stained_glass"}
COLORS = {"player_legs": (0.15, 0.2, 0.55), "player_body": (0.0, 0.6, 0.65)}

MODE = argv[3] if len(argv) > 3 else ""
SHIP_ONLY = MODE in ("ship_only", "intact")
if MODE == "intact":
    data["ship"] = data["intact"]
blocks = {}
for x, y, z, b in data["ship"]:
    blocks[(x, y, z)] = b
if not SHIP_ONLY:
    for x, y, z, b in data["terrain"]:
        blocks.setdefault((x, y, z), b)
    for x, y, z, b in data["figure"]:
        blocks[(x, y, z)] = b


CROSS = {"fire": "fire_0", "wheat": "wheat", "carrots": "carrots"}


def opaque(b):
    return b is not None and b not in TRANSLUCENT and b not in CROSS and b not in THIN and not b.startswith("ladder_")


# Minecraft (x,y,z) -> Blender (x,-z,y)
def bv(x, y, z):
    return (x, -z, y)


# face: (normal, 4 corner offsets in MC coords, which-texture idx 0 top 1 bottom 2 side)
QUADS = {
    (0, 1, 0): ([(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)], 0),
    (0, -1, 0): ([(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)], 1),
    (1, 0, 0): ([(1, 0, 1), (1, 0, 0), (1, 1, 0), (1, 1, 1)], 2),
    (-1, 0, 0): ([(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)], 2),
    (0, 0, 1): ([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], 2),
    (0, 0, -1): ([(1, 0, 0), (0, 0, 0), (0, 1, 0), (1, 1, 0)], 2),
}
UV = [(0, 0), (1, 0), (1, 1), (0, 1)]

verts, faces, mats, uvs = [], [], [], []
mat_index = {}


def mat_of(tex):
    if tex not in mat_index:
        mat_index[tex] = len(mat_index)
    return mat_index[tex]


def add_quad(corners, tex):
    i = len(verts)
    verts.extend(corners)
    faces.append((i, i + 1, i + 2, i + 3))
    mats.append(mat_of(tex))
    uvs.extend(UV)


for (x, y, z), b in blocks.items():
    if b in CROSS:  # two crossed planes, like the game draws fire and crops
        for a, c in (((0.15, 0.15), (0.85, 0.85)), ((0.85, 0.15), (0.15, 0.85))):
            q = [(a[0], 0, a[1]), (c[0], 0, c[1]), (c[0], 1, c[1]), (a[0], 1, a[1])]
            add_quad([bv(x + u, y + v, z + w) for u, v, w in q], CROSS[b])
        continue
    if b.startswith("ladder_"):  # a flat panel just off the wall it hangs on
        o = {"e": ((0.94, 0, 0), (0.94, 0, 1)), "w": ((0.06, 0, 1), (0.06, 0, 0)),
             "s": ((1, 0, 0.94), (0, 0, 0.94)), "n": ((0, 0, 0.06), (1, 0, 0.06))}[b[-1]]
        (a0, _, a2), (c0, _, c2) = o
        q = [(a0, 0, a2), (c0, 0, c2), (c0, 1, c2), (a0, 1, a2)]
        add_quad([bv(x + u, y + v, z + w) for u, v, w in q], "ladder")
        continue
    spec = FACES.get(b)
    for n, (corners, ti) in QUADS.items():
        nb = blocks.get((x + n[0], y + n[1], z + n[2]))
        if (opaque(nb) and (b not in THIN or n == (0, -1, 0))) or (b in TRANSLUCENT and nb == b):
            continue
        if spec is None:
            tex = "color:" + b
        elif isinstance(spec, str):
            tex = spec
        else:
            tex = spec[ti]
        hgt = THIN.get(b, 1.0)
        if hgt < 1 and n == (0, -1, 0) and opaque(nb):
            continue
        if hgt < 1 and n != (0, -1, 0):
            pass  # thin blocks keep their side/top faces even beside solids
        add_quad([bv(x + u, y + v * hgt, z + w) for u, v, w in corners], tex)

bpy.ops.wm.read_factory_settings(use_empty=True)
mesh = bpy.data.meshes.new("blocks")
mesh.from_pydata(verts, [], faces)
mesh.update()
uvl = mesh.uv_layers.new(name="UVMap")
for li, loop in enumerate(mesh.loops):
    uvl.data[li].uv = uvs[loop.vertex_index]
for pi, p in enumerate(mesh.polygons):
    p.material_index = mats[pi]
obj = bpy.data.objects.new("blocks", mesh)
bpy.context.scene.collection.objects.link(obj)

for tex, idx in sorted(mat_index.items(), key=lambda kv: kv[1]):
    m = bpy.data.materials.new(tex)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.85
    bsdf.inputs["Specular IOR Level"].default_value = 0.2
    if tex.startswith("color:"):
        c = COLORS.get(tex[6:], (1, 0, 1))
        bsdf.inputs["Base Color"].default_value = (*c, 1)
    else:
        img = bpy.data.images.load(str(TEX / f"{tex}.png"))
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = img
        t.interpolation = "Closest"
        nt.links.new(t.outputs["Color"], bsdf.inputs["Base Color"])
        if tex in ("glass_light_blue", "fire_0", "glass", "ladder", "wheat", "carrots", "lantern", "glass_red", "glass_lime", "water"):
            nt.links.new(t.outputs["Alpha"], bsdf.inputs["Alpha"])
            m.blend_method = "BLEND"
        if tex in ("glass_light_blue", "glass", "glass_red", "glass_lime"):
            bsdf.inputs["Roughness"].default_value = 0.05
        if tex in EMIT:
            nt.links.new(t.outputs["Color"], bsdf.inputs["Emission Color"])
            bsdf.inputs["Emission Strength"].default_value = EMIT[tex]
        if tex == "iron_block":
            bsdf.inputs["Metallic"].default_value = 0.5
            bsdf.inputs["Roughness"].default_value = 0.45
    mesh.materials.append(m)

scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
import os
scene.cycles.samples = int(os.environ.get("SAMPLES", 24))
scene.cycles.use_denoising = False
scene.cycles.max_bounces = 4
scene.render.resolution_x = int(os.environ.get("RESX", 1280))
scene.render.resolution_y = scene.render.resolution_x * 9 // 16
scene.view_settings.exposure = float(os.environ.get("EXPOSURE", -0.6))
scene.view_settings.view_transform = "AgX" if "AgX" in [i.identifier for i in scene.view_settings.bl_rna.properties["view_transform"].enum_items] else "Filmic"

try:
    scene.view_settings.look = "AgX - Medium High Contrast"
except TypeError:
    pass
world = bpy.data.worlds.new("w")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes["Background"]
sky = world.node_tree.nodes.new("ShaderNodeTexSky")
sky.sky_type = "NISHITA"
sky.sun_elevation = math.radians(float(os.environ.get("SUN_ELEV", 32)))
sky.sun_rotation = math.radians(140)
world.node_tree.links.new(sky.outputs["Color"], bg.inputs["Color"])
bg.inputs["Strength"].default_value = float(os.environ.get("SKY", 0.22))

if SHIP_ONLY:
    zmin = min(-0.0 + y for x, y, z, _ in data["ship"])
    bpy.ops.mesh.primitive_plane_add(size=400, location=(0, 0, zmin))
    pm = bpy.data.materials.new("floor")
    pm.use_nodes = True
    pm.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.05, 0.055, 0.06, 1)
    bpy.context.object.data.materials.append(pm)

sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
sun.data.energy = 2.2
sun.data.angle = math.radians(2)
sun.rotation_euler = (math.radians(90 - float(os.environ.get("SUN_ELEV", 32))), 0, math.radians(140 + 90))
if float(os.environ.get("SUN_ELEV", 32)) < 12:
    sun.data.energy = 1.2
    sun.data.color = (1.0, 0.62, 0.38)
scene.collection.objects.link(sun)

if os.environ.get("FILL"):
    # Minecraft lights undersides at about half; a soft upward light stands in for it
    fill = bpy.data.objects.new("fill", bpy.data.lights.new("fill", "SUN"))
    fill.data.energy = float(os.environ["FILL"])
    fill.data.angle = math.radians(40)
    fill.rotation_euler = (math.radians(180 - 30), 0, math.radians(60))
    scene.collection.objects.link(fill)
if os.environ.get("FAR_GROUND"):
    gy = float(os.environ["FAR_GROUND"])
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, gy))
    gp = bpy.context.object
    gp.scale = (900, 900, 1)
    bpy.ops.object.transform_apply(scale=True)
    for loop in gp.data.loops:
        uv = gp.data.uv_layers.active.data[loop.index].uv
        uv.x *= 900
        uv.y *= 900
    gm = bpy.data.materials.new("far_grass")
    gm.use_nodes = True
    tn = gm.node_tree.nodes.new("ShaderNodeTexImage")
    tn.image = bpy.data.images.load(str(TEX / "grass_top.png"))
    tn.interpolation = "Closest"
    gm.node_tree.links.new(tn.outputs["Color"], gm.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
    gm.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.9
    gp.data.materials.append(gm)
    gp.visible_diffuse = False  # distant filler: seen, but no green bounce onto the ship
    gp.visible_glossy = False

if data.get("smoke") and os.environ.get("SMOKE", "1") == "1":
    # stand-in for campfire particles: a soft grey volume column per campfire
    sm = bpy.data.materials.new("smoke")
    sm.use_nodes = True
    nt = sm.node_tree
    nt.nodes.remove(nt.nodes["Principled BSDF"])
    vol = nt.nodes.new("ShaderNodeVolumePrincipled")
    vol.inputs["Color"].default_value = (0.45, 0.45, 0.47, 1)
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 0.6
    ramp = nt.nodes.new("ShaderNodeMapRange")
    ramp.inputs["From Min"].default_value = 0.45
    ramp.inputs["To Max"].default_value = 0.9
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Value"])
    nt.links.new(ramp.outputs["Result"], vol.inputs["Density"])
    nt.links.new(vol.outputs["Volume"], nt.nodes["Material Output"].inputs["Volume"])
    for sx, sy, sz, signal in data["smoke"]:
        hgt = 24 if signal else 9
        bpy.ops.mesh.primitive_cone_add(vertices=12, radius1=0.7, radius2=2.6, depth=hgt,
                                        location=(sx + 1.5, -sz - 0.8, sy + hgt / 2))
        c = bpy.context.object
        c.rotation_euler = (math.radians(4), math.radians(9), 0)  # a light drift
        c.data.materials.append(sm)

cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
cam.data.lens = 30
cam.data.clip_end = 3000
scene.collection.objects.link(cam)
scene.camera = cam

ship = [bv(x + 0.5, y + 0.5, z + 0.5) for x, y, z, _ in data["ship"]]
cx = sum(p[0] for p in ship) / len(ship)
cy = sum(p[1] for p in ship) / len(ship)
cz = sum(p[2] for p in ship) / len(ship)
center = Vector((cx, cy, cz))


def shoot(name, offset, target=None, lens=30):
    cam.data.lens = lens
    cam.location = center + Vector(offset)
    d = (target or center) - cam.location
    cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = str(OUT / f"{name}.png")
    bpy.ops.render.render(write_still=True)


views = argv[4].split(",") if len(argv) > 4 else None
SHOTS = {
    "s_side": ((0.01, -70, 4), None, 35),
    "s_three": ((40, -45, 30), None, 32),
    "s_top": ((0.01, 0.01, 85), None, 32),
    "s_rear": ((-55, -25, 12), None, 32),
    "hero": ((-14, -52, 20), None, 30),
    "approach": ((-80, 50, 14), None, 30),
    "breach": ((-6, -40, 10), None, 35),
    "trail": ((-75, 40, 30), None, 24),
    "top": ((0.1, 0.1, 110), None, 30),
    "front": ((45, 10, 8), None, 32),
}
def shoot_abs(name, loc, tgt, lens):
    cam.data.lens = lens
    cam.location = Vector(loc)
    d = Vector(tgt) - cam.location
    cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = str(OUT / f"{name}.png")
    s0 = scene.cycles.samples
    if name == "inside" or name.startswith("in_"):
        scene.cycles.samples = s0 * 3  # mostly bounce light in there
    bpy.ops.render.render(write_still=True)
    scene.cycles.samples = s0


key = "shots_intact" if MODE == "intact" else "shots"
if key in data:
    for name, (loc, tgt, lens) in data[key].items():
        if views is None or name in views:
            shoot_abs(name, loc, tgt, lens)
else:
    for name, (off, tgt, lens) in SHOTS.items():
        if views is None or name in views:
            shoot(name, off, tgt, lens)
