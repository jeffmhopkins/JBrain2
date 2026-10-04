# One photoreal shot in Blender (Cycles): the printed parts from OpenSCAD, the board and panel
# from Waveshare's STEP, the firmware's own pet on the glass. make_photoreal.sh drives it.
# Usage: python hero.py <plate|stand> <work dir> <out.png> [width] [samples] [camera x,y,z]
# A camera position renders a check view from there, aimed at the middle of the unit.
import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Matrix, Vector

shot, work, out = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
width = int(sys.argv[4]) if len(sys.argv) > 4 else 1600
samples = int(sys.argv[5]) if len(sys.argv) > 5 else 256
parts = work / shot
poses = {k: Matrix(v) for k, v in json.loads((parts / "poses.json").read_text()).items()}

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.unit_settings.scale_length = 0.001  # 1 unit = 1 mm: the glTF importer and DOF honour it


def Tz(z):
    return Matrix.Translation((0, 0, z))


# ---------- materials ----------
def principled(name, color, rough, metal=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = metal
    return m, b


def pla(color):
    """Matte PLA with 0.2 mm layer lines along the part's own print Z, so every part must come
    in in its print frame and be posed by its object matrix."""
    m, b = principled("PLA", color, 0.48)
    b.inputs["Specular IOR Level"].default_value = 0.45
    nt = m.node_tree
    tc = nt.nodes.new("ShaderNodeTexCoord")
    sep = nt.nodes.new("ShaderNodeSeparateXYZ")
    nt.links.new(tc.outputs["Object"], sep.inputs[0])
    per_layer = nt.nodes.new("ShaderNodeMath")
    per_layer.operation = "MULTIPLY"
    per_layer.inputs[1].default_value = 2 * math.pi / 0.2
    nt.links.new(sep.outputs["Z"], per_layer.inputs[0])
    ridge = nt.nodes.new("ShaderNodeMath")
    ridge.operation = "SINE"
    nt.links.new(per_layer.outputs[0], ridge.inputs[0])
    # A little low-frequency wobble, so the lines are not machined-perfect.
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 0.6
    nt.links.new(tc.outputs["Object"], noise.inputs["Vector"])
    height = nt.nodes.new("ShaderNodeMath")
    height.operation = "MULTIPLY_ADD"
    height.inputs[1].default_value = 0.04
    nt.links.new(noise.outputs["Fac"], height.inputs[0])
    nt.links.new(ridge.outputs[0], height.inputs[2])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.35
    bump.inputs["Distance"].default_value = 0.015
    nt.links.new(height.outputs[0], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return m


mat_pla = pla((0.86, 0.20, 0.07))  # a warm coral-orange filament
mat_shell, _ = principled("Shell", (0.012, 0.012, 0.013), 0.42)
mat_buttons, _ = principled("Buttons", (0.05, 0.05, 0.055), 0.6)
mat_glass, glass_bsdf = principled("Glass", (0.004, 0.004, 0.005), 0.04)
glass_bsdf.inputs["Specular IOR Level"].default_value = 0.6
mat_metal, _ = principled("Steel", (0.75, 0.75, 0.76), 0.22, metal=1.0)
mat_screen, screen_bsdf = principled("Screen", (0, 0, 0), 0.04)
screen_bsdf.inputs["Specular IOR Level"].default_value = 0.6
tex = mat_screen.node_tree.nodes.new("ShaderNodeTexImage")
tex.image = bpy.data.images.load(
    str(work / f"screen_{'portrait' if shot == 'plate' else 'landscape'}.png"))
tex.interpolation = "Closest"  # the panel's own pixels, not a blur of them
mat_screen.node_tree.links.new(tex.outputs["Color"], screen_bsdf.inputs["Emission Color"])
screen_bsdf.inputs["Emission Strength"].default_value = 1.5


# ---------- the printed parts, the shell, the screws ----------
def stl(name, mat, pose=Matrix.Identity(4)):
    bpy.ops.wm.stl_import(filepath=str(parts / f"{name}.stl"))
    o = bpy.context.selected_objects[0]
    o.data.materials.clear()
    o.data.materials.append(mat)
    o.matrix_world = pose
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.shade_smooth_by_angle(angle=math.radians(35))
    # A hair of bevel: printed edges are never knife-sharp, and they catch the light.
    bev = o.modifiers.new("bevel", "BEVEL")
    bev.width, bev.segments = 0.12, 2
    bev.limit_method, bev.angle_limit = "ANGLE", math.radians(40)
    bev.harden_normals = True


if shot == "plate":
    seam = poses["marker_plate"]
    stl("plate", mat_pla)
else:
    seam = poses["marker_seam"]
    stl("box", mat_pla, poses["marker_box"])
    stl("plate", mat_pla, poses["marker_head"])
    stl("screws", mat_metal, poses["marker_box"])
stl("shell", mat_shell, seam)
stl("buttons", mat_buttons, seam)

# ---------- the board and panel, from Waveshare's STEP ----------
before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=str(work / "module.glb"))
module_objs = [o for o in bpy.data.objects if o not in before]
root = bpy.data.objects.new("module", None)
scene.collection.objects.link(root)
# The glTF round trip leaves the STEP's z as Blender's -y: (bx, by, bz) is STEP (bx, bz, -by).
to_step = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))
# The board's back (STEP z -7.4) hangs on standoffs that end at the seam, 4 mm below it.
step = seam @ Tz(11.4)
root.matrix_world = step @ to_step
for o in module_objs:
    if o.parent is None:
        o.parent, o.matrix_parent_inverse = root, Matrix.Identity(4)
for o in module_objs:
    name = o.name.upper()
    if o.type == "MESH" and ("TYPE-C" in name or "SWITCH" in name):
        o.data.materials.clear()
        o.data.materials.append(mat_metal if "TYPE-C" in name else mat_shell)

# The panel is one STEP solid; its glass is the upward faces at STEP z -0.05.
panel = bpy.data.objects["SPEC-DO0180FMST08"]
me = panel.data
me.materials.clear()
me.materials.append(mat_shell)
me.materials.append(mat_glass)
bpy.context.view_layer.update()
in_step = step.inverted() @ panel.matrix_world
lo, hi = Vector((1e9, 1e9)), Vector((-1e9, -1e9))
for p in me.polygons:
    is_glass = (in_step.to_3x3() @ p.normal).z > 0.99 and abs((in_step @ p.center).z + 0.05) < 0.03
    p.material_index = int(is_glass)
    if is_glass:
        for v in p.vertices:
            q = in_step @ me.vertices[v].co
            lo.x, lo.y, hi.x, hi.y = min(lo.x, q.x), min(lo.y, q.y), max(hi.x, q.x), max(hi.y, q.y)
me.polygons.foreach_set("use_smooth", [False] * len(me.polygons))

# The lit area, centred under the glass: 368 x 448 px on a 1.8" panel is 0.0789 mm a pixel.
pw, ph = 368 * 0.0789, 448 * 0.0789
cx, cy = (lo.x + hi.x) / 2, (lo.y + hi.y) / 2
bm = bmesh.new()
uv = bm.loops.layers.uv.new()
face = bm.faces.new([bm.verts.new((cx + sx * pw / 2, cy + sy * ph / 2, -0.04))
                     for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
for loop, coord in zip(face.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
    loop[uv].uv = coord  # image columns along STEP +x, its top row at STEP +y
screen_mesh = bpy.data.meshes.new("screen")
bm.to_mesh(screen_mesh)
screen_mesh.materials.append(mat_screen)
screen = bpy.data.objects.new("screen", screen_mesh)
scene.collection.objects.link(screen)
screen.matrix_world = step

# ---------- the unit in the world: on the floor, centred ----------
hero = bpy.data.objects.new("hero", None)
scene.collection.objects.link(hero)
for o in list(scene.objects):
    if o.parent is None and o is not hero:
        o.parent, o.matrix_parent_inverse = hero, Matrix.Identity(4)
if shot == "plate":
    # Stood on its bottom edge, the glass toward the camera, the pet upright.
    hero.matrix_world = Matrix.Rotation(math.radians(90), 4, "X")


def corners():
    bpy.context.view_layer.update()
    return [o.matrix_world @ Vector(c) for o in scene.objects if o.type == "MESH" for c in o.bound_box]


pts = corners()
hero.matrix_world = Matrix.Translation((
    -(min(p.x for p in pts) + max(p.x for p in pts)) / 2,
    -(min(p.y for p in pts) + max(p.y for p in pts)) / 2,
    -min(p.z for p in pts))) @ hero.matrix_world
h = max(p.z for p in corners())

# ---------- studio: a seamless sweep, three soft lights ----------
bm = bmesh.new()
r, length, back, front = 120, 900, 260, -700
profile = ([(front, 0)]
           + [(back - r + r * math.sin(a), r - r * math.cos(a))
              for a in (i * math.pi / 48 for i in range(25))]
           + [(back, 600)])
rows = [[bm.verts.new((x, y, z)) for y, z in profile] for x in (-length / 2, length / 2)]
for i in range(len(profile) - 1):
    bm.faces.new((rows[0][i], rows[1][i], rows[1][i + 1], rows[0][i + 1]))
sweep_mesh = bpy.data.meshes.new("sweep")
bm.to_mesh(sweep_mesh)
sweep_mesh.shade_smooth()
sweep_mesh.materials.append(principled("Backdrop", (0.30, 0.29, 0.28), 0.9)[0])
scene.collection.objects.link(bpy.data.objects.new("sweep", sweep_mesh))


def area(name, loc, size, power, color=(1, 1, 1), size_y=None):
    light = bpy.data.lights.new(name, "AREA")
    light.energy, light.color = power, color
    light.shape, light.size, light.size_y = "RECTANGLE", size, size_y or size
    o = bpy.data.objects.new(name, light)
    scene.collection.objects.link(o)
    o.location = loc
    o.rotation_euler = (Vector((0, 0, h * 0.45)) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()


warm, cool = (1.0, 0.96, 0.92), (0.9, 0.95, 1.0)
if shot == "plate":
    cam_loc, look = Vector((120, -215, 120)), Vector((3, 0, h * 0.40))
    area("key", (-160, -150, 210), 160, 1.6e6, warm)
    area("rim", (170, 160, 120), 70, 1.6e6, cool, 220)
    area("fill", (200, -120, 40), 200, 0.25e6)
else:
    cam_loc, look = Vector((220, -120, 165)), Vector((8, 0, h * 0.32))
    area("key", (-60, -230, 230), 170, 1.6e6, warm)
    area("rim", (-170, 150, 140), 70, 1.6e6, cool, 220)
    # Low and to the side: the glass faces up here, and a fill behind the camera greys it out.
    area("fill", (120, -260, 30), 200, 0.3e6)
if len(sys.argv) > 6:
    cam_loc, look = Vector(map(float, sys.argv[6].split(","))), Vector((0, 0, h * 0.5))

cam_data = bpy.data.cameras.new("cam")
cam_data.lens, cam_data.sensor_width = 85, 36
cam_data.dof.use_dof = True
cam_data.dof.focus_distance = (look - cam_loc).length
cam_data.dof.aperture_fstop = 8
cam_data.clip_start, cam_data.clip_end = 1, 5000
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
cam.location = cam_loc
cam.rotation_euler = (look - cam_loc).to_track_quat("-Z", "Y").to_euler()
scene.camera = cam

scene.world = bpy.data.worlds.new("world")
scene.world.use_nodes = True
bg = scene.world.node_tree.nodes["Background"]
bg.inputs["Color"].default_value = (0.5, 0.5, 0.52, 1)
bg.inputs["Strength"].default_value = 0.25

# ---------- render ----------
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = samples
scene.cycles.use_adaptive_sampling = True
scene.cycles.use_denoising = True
scene.render.resolution_x, scene.render.resolution_y = width, int(width * 0.66)
scene.view_settings.view_transform = "AgX"
scene.view_settings.look = "AgX - Punchy"
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = out
bpy.ops.render.render(write_still=True)
