"""Crop Mojang's vanilla Bedrock block textures (bedrock-samples) to one 16x16
frame each, applying the plains biome tint where the game would."""

import sys
from pathlib import Path

from PIL import Image

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(exist_ok=True)

GRASS_TINT = (145, 189, 89)

TEX = {
    "concrete_white": "concrete_white.png",
    "concrete_silver": "concrete_silver.png",
    "concrete_gray": "concrete_gray.png",
    "concrete_black": "concrete_black.png",
    "concrete_orange": "concrete_orange.png",
    "iron_block": "iron_block.png",
    "polished_deepslate": "deepslate/polished_deepslate.png",
    "blackstone": "blackstone.png",
    "blackstone_top": "blackstone_top.png",
    "coal_block": "coal_block.png",
    "glass_light_blue": "glass_light_blue.png",
    "sea_lantern": "sea_lantern.png",
    "redstone_lamp_on": "redstone_lamp_on.png",
    "magma": "magma.png",
    "stone_slab_top": "stone_slab_top.png",
    "barrel_side": "barrel_side.png",
    "barrel_top": "barrel_top.png",
    "barrel_bottom": "barrel_bottom.png",
    "grass_top": "grass_top.png",
    "grass_side": "grass_side_carried.png",
    "dirt": "dirt.png",
    "coarse_dirt": "coarse_dirt.png",
    "gravel": "gravel.png",
    "stone": "stone.png",
    "log_oak": "log_oak.png",
    "log_oak_top": "log_oak_top.png",
    "leaves_oak": "leaves_oak.tga",
    "fire_0": "fire_0.png",
    "sandstone_top": "sandstone_top.png",
    "sandstone_side": "sandstone_smooth.png",
    "log_jungle": "log_jungle.png",
    "log_jungle_top": "log_jungle_top.png",
    "leaves_jungle": "leaves_jungle.tga",
    "log_spruce": "log_spruce.png",
    "log_spruce_top": "log_spruce_top.png",
    "leaves_spruce": "leaves_spruce.tga",
    "andesite": "stone_andesite.png",
    "cobblestone": "cobblestone.png",
    "ochre_side": "ochre_froglight_side.png",
    "ochre_top": "ochre_froglight_top.png",
    "snow": "snow.png",
    "water": "water_still_grey.png",
    "moss": "moss_block.png",
    "sand": "sand.png",
    "red_sand": "red_sand.png",
    "mud": "mud.png",
    "packed_mud": "packed_mud.png",
    "terracotta_orange": "hardened_clay_stained_orange.png",
    "red_sandstone_top": "red_sandstone_top.png",
    "red_sandstone_side": "red_sandstone_smooth.png",
    "ladder": "ladder.png",
    "bookshelf": "bookshelf.png",
    "tnt_side": "tnt_side.png",
    "tnt_top": "tnt_top.png",
    "farmland": "farmland_wet.png",
    "wheat": "wheat_stage_7.png",
    "carrots": "carrots_stage_3.png",
    "wool_red": "wool_colored_red.png",
    "smithing_top": "smithing_table_top.png",
    "smithing_side": "smithing_table_side.png",
    "anvil_top": "anvil_top_damaged_0.png",
    "anvil_base": "anvil_base.png",
    "lantern": "lantern.png",
    "glass": "glass.png",
    "glass_red": "glass_red.png",
    "glass_lime": "glass_lime.png",
    "campfire_log_lit": "campfire_log_lit.png",
    "hay_top": "hay_block_top.png",
    "hay_side": "hay_block_side.png",
    "deepslate_tiles": "deepslate/deepslate_tiles.png",
    "deepslate_bricks": "deepslate/deepslate_bricks.png",
    "polished_blackstone": "polished_blackstone.png",
    "shroomlight": "shroomlight.png",
    "glowstone": "glowstone.png",
    "polished_basalt_side": "polished_basalt_side.png",
    "polished_basalt_top": "polished_basalt_top.png",
    "concrete_light_blue": "concrete_light_blue.png",
    "concrete_cyan": "concrete_cyan.png",
    "wool_white": "wool_colored_white.png",
    "wool_orange": "wool_colored_orange.png",
    "crafting_table_top": "crafting_table_top.png",
    "crafting_table_side": "crafting_table_side.png",
    "crafting_table_front": "crafting_table_front.png",
    "furnace_front": "furnace_front_on.png",
    "furnace_side": "furnace_side.png",
    "furnace_top": "furnace_top.png",
    "iron_trapdoor": "iron_trapdoor.png",
    "bed_head_top": "bed_head_top.png",
    "bed_feet_top": "bed_feet_top.png",
    "bed_head_side": "bed_head_side.png",
    "planks_oak": "planks_oak.png",
}


def tint(im, rgb, mask=None):
    px = im.load()
    m = mask.load() if mask else None
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if m is None or m[x, y][3] > 0:
                px[x, y] = (r * rgb[0] // 255, g * rgb[1] // 255, b * rgb[2] // 255, a)
    return im


for name, f in TEX.items():
    im = Image.open(SRC / f).convert("RGBA")
    im = im.crop((0, 0, 16, 16))  # first frame of animated strips
    if name in ("leaves_jungle", "leaves_spruce"):
        im = tint(im, (48, 187, 11) if name == "leaves_jungle" else (97, 153, 97))
        im.putalpha(255)
    if name == "water":
        im = tint(im, (63, 118, 228))
        im.putalpha(150)
    if name in ("grass_top", "leaves_oak"):
        im = tint(im, GRASS_TINT if name == "grass_top" else (119, 171, 47))
        if name == "leaves_oak":
            im.putalpha(255)  # fancy-leaves holes don't matter at preview scale
    im.save(OUT / f"{name}.png")
print("ok", len(TEX))
