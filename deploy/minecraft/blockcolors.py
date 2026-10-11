"""What colour a block reads as from above, for the map's explored ground (plan §M8,
owner request 2026-10-11: "the colours of the real blocks, instead of just biome").

Colours are each block's top texture averaged by eye, the way a player pictures it, not
the 62-entry palette of an in-game map item: a concrete hull, a wheat field and a cherry
grove should each read as themselves. Families that differ only by dye or wood type are
rules rather than rows, so a new block in a known family still colours correctly.

`color` answers one of three ways: an RGB, `CLEAR` for a block the eye looks straight
through from above (air, glass, a torch), or None for a block this table doesn't know,
which the map draws in the column's biome colour rather than guessing.
"""

from __future__ import annotations

RGB = tuple[int, int, int]
CLEAR: RGB = (-1, -1, -1)

DYES: dict[str, tuple[RGB, RGB, RGB]] = {
    # dye: (concrete, wool, terracotta)
    "white": ((207, 213, 214), (234, 236, 237), (210, 178, 161)),
    "orange": ((224, 97, 1), (241, 118, 20), (162, 84, 38)),
    "magenta": ((169, 48, 159), (190, 69, 180), (150, 88, 109)),
    "light_blue": ((36, 137, 199), (58, 175, 217), (113, 109, 138)),
    "yellow": ((241, 175, 21), (249, 198, 40), (186, 133, 35)),
    "lime": ((94, 169, 24), (112, 185, 26), (104, 118, 53)),
    "pink": ((214, 101, 143), (238, 141, 172), (162, 78, 79)),
    "gray": ((55, 58, 62), (63, 68, 72), (58, 42, 36)),
    "light_gray": ((125, 125, 115), (142, 142, 135), (135, 107, 98)),
    "cyan": ((21, 119, 136), (21, 138, 145), (87, 91, 91)),
    "purple": ((100, 32, 156), (122, 42, 173), (118, 70, 86)),
    "blue": ((45, 47, 143), (53, 57, 157), (74, 60, 91)),
    "brown": ((96, 60, 32), (114, 72, 41), (77, 51, 36)),
    "green": ((73, 91, 36), (85, 110, 28), (76, 83, 42)),
    "red": ((142, 33, 33), (161, 39, 35), (143, 61, 47)),
    "black": ((8, 10, 15), (21, 21, 26), (37, 23, 16)),
}
# Longest first, so `light_blue_wool` isn't read as `blue`.
_DYE_ORDER = sorted(DYES, key=len, reverse=True)

# What each dyed family shows from above: which of the three swatches, lightened or not.
_DYED: dict[str, tuple[int, float]] = {
    "concrete": (0, 0.0),
    "concrete_powder": (0, 0.15),
    "glazed_terracotta": (0, 0.1),
    "wool": (1, 0.0),
    "carpet": (1, 0.0),
    "bed": (1, 0.0),
    "banner": (1, 0.0),
    "candle": (1, 0.0),
    "shulker_box": (1, -0.1),
    "stained_glass": (1, 0.25),
    "stained_glass_pane": (1, 0.25),
    "terracotta": (2, 0.0),
}

WOODS: dict[str, RGB] = {
    # wood: its planks; the log's cut top and stripped wood read the same from above
    "oak": (162, 130, 78),
    "spruce": (114, 84, 48),
    "birch": (192, 175, 121),
    "jungle": (160, 115, 80),
    "acacia": (168, 90, 50),
    "dark_oak": (66, 43, 20),
    "mangrove": (117, 54, 48),
    "cherry": (226, 178, 172),
    "pale_oak": (227, 217, 215),
    "bamboo": (193, 173, 80),
    "crimson": (101, 48, 70),
    "warped": (43, 104, 99),
}
_WOOD_ORDER = sorted(WOODS, key=len, reverse=True)
_WOODEN = (
    "planks", "slab", "double_slab", "stairs", "fence", "fence_gate", "door", "trapdoor",
    "pressure_plate", "standing_sign", "wall_sign", "hanging_sign", "log", "wood",
    "stem", "hyphae", "block", "mosaic", "mosaic_slab", "mosaic_stairs",
)
LEAVES: dict[str, RGB] = {
    "oak": (59, 122, 30), "jungle": (48, 128, 25), "acacia": (70, 120, 28),
    "dark_oak": (50, 100, 25), "mangrove": (62, 118, 40), "spruce": (56, 88, 56),
    "birch": (98, 128, 62), "cherry": (231, 171, 199), "azalea": (90, 115, 45),
    "flowering_azalea": (110, 115, 75), "pale_oak": (140, 145, 130),
}

STONE: RGB = (125, 125, 125)
WATER: RGB = (52, 92, 190)
# Blocks the eye passes through from above: the map shows what is under them.
_CLEAR = frozenset({
    "air", "cave_air", "void_air", "glass", "glass_pane", "hard_glass", "hard_glass_pane",
    "tinted_glass", "barrier", "structure_void", "light_block", "torch", "wall_torch",
    "soul_torch", "soul_wall_torch", "redstone_torch", "unlit_redstone_torch",
    "tripwire", "trip_wire", "tripwire_hook", "string", "lever", "stone_button",
    "wooden_button", "redstone_wire", "rail", "golden_rail", "detector_rail",
    "activator_rail", "end_rod", "lightning_rod", "chain", "iron_bars", "ladder",
    "scaffolding", "cobweb", "web", "structure_block", "moving_block",
})

BLOCKS: dict[str, RGB] = {
    # ground
    "grass_block": (106, 160, 58), "grass": (106, 160, 58), "dirt": (134, 96, 67),
    "coarse_dirt": (119, 85, 59), "rooted_dirt": (144, 103, 76), "podzol": (91, 63, 24),
    "mycelium": (111, 99, 105), "dirt_with_roots": (144, 103, 76),
    "grass_path": (148, 122, 65), "dirt_path": (148, 122, 65), "farmland": (82, 54, 30),
    "mud": (60, 57, 61), "packed_mud": (142, 106, 79), "mud_bricks": (137, 103, 79),
    "clay": (160, 166, 179), "gravel": (131, 127, 126), "suspicious_gravel": (131, 127, 126),
    "sand": (219, 207, 163), "suspicious_sand": (219, 207, 163), "red_sand": (190, 102, 33),
    "snow": (249, 254, 254), "snow_layer": (249, 254, 254), "powder_snow": (248, 253, 253),
    "ice": (145, 183, 253), "packed_ice": (141, 180, 250), "blue_ice": (116, 167, 253),
    "frosted_ice": (145, 183, 253), "moss_block": (89, 109, 45), "moss_carpet": (89, 109, 45),
    "pale_moss_block": (106, 112, 104), "pale_moss_carpet": (106, 112, 104),
    "sculk": (12, 29, 36), "sculk_vein": (12, 29, 36), "sculk_catalyst": (15, 35, 40),
    "sculk_sensor": (7, 70, 84), "sculk_shrieker": (198, 205, 169),
    # water and lava; water is shaded deeper by the map, not here
    "water": WATER, "flowing_water": WATER, "bubble_column": WATER,
    "lava": (207, 92, 20), "flowing_lava": (207, 92, 20), "magma": (142, 63, 31),
    # stone
    "stone": STONE, "cobblestone": (127, 127, 127), "mossy_cobblestone": (110, 118, 94),
    "smooth_stone": (159, 159, 159), "stone_bricks": (122, 121, 122),
    "mossy_stone_bricks": (115, 121, 105), "cracked_stone_bricks": (118, 117, 118),
    "chiseled_stone_bricks": (120, 119, 120), "granite": (149, 103, 85),
    "polished_granite": (154, 106, 89), "diorite": (188, 188, 188),
    "polished_diorite": (192, 193, 194), "andesite": (136, 136, 136),
    "polished_andesite": (132, 134, 133), "deepslate": (80, 80, 82),
    "cobbled_deepslate": (77, 77, 80), "polished_deepslate": (72, 72, 73),
    "deepslate_bricks": (70, 70, 71), "deepslate_tiles": (54, 54, 55),
    "chiseled_deepslate": (54, 54, 54), "tuff": (108, 109, 102),
    "polished_tuff": (98, 104, 99), "tuff_bricks": (98, 102, 95), "calcite": (223, 224, 220),
    "dripstone_block": (134, 107, 92), "pointed_dripstone": (129, 102, 89),
    "bedrock": (85, 85, 85), "obsidian": (15, 10, 24), "crying_obsidian": (32, 10, 60),
    "sandstone": (216, 203, 155), "smooth_sandstone": (223, 214, 170),
    "cut_sandstone": (217, 206, 159), "chiseled_sandstone": (216, 202, 155),
    "red_sandstone": (186, 99, 29), "smooth_red_sandstone": (181, 97, 31),
    "cut_red_sandstone": (189, 101, 31), "chiseled_red_sandstone": (183, 96, 27),
    "brick_block": (150, 97, 83), "bricks": (150, 97, 83), "terracotta": (152, 94, 68),
    "hardened_clay": (152, 94, 68), "prismarine": (99, 156, 151),
    "prismarine_bricks": (99, 171, 158), "dark_prismarine": (51, 91, 75),
    "sea_lantern": (172, 199, 190), "quartz_block": (235, 229, 222),
    "smooth_quartz": (235, 229, 222), "quartz_bricks": (234, 229, 221),
    "chiseled_quartz_block": (231, 226, 218), "quartz_pillar": (235, 230, 224),
    "end_stone": (219, 222, 158), "end_bricks": (218, 224, 162),
    "purpur_block": (169, 125, 169), "purpur_pillar": (171, 129, 171),
    # the Nether
    "netherrack": (97, 38, 38), "nether_bricks": (44, 21, 26),
    "red_nether_bricks": (69, 7, 9), "nether_wart_block": (114, 2, 2),
    "warped_wart_block": (22, 119, 121), "crimson_nylium": (130, 31, 31),
    "warped_nylium": (43, 114, 101), "soul_sand": (81, 62, 50), "soul_soil": (75, 57, 46),
    "basalt": (80, 81, 86), "polished_basalt": (99, 98, 100), "smooth_basalt": (72, 72, 78),
    "blackstone": (42, 36, 41), "polished_blackstone": (53, 48, 56),
    "polished_blackstone_bricks": (48, 42, 49), "gilded_blackstone": (55, 42, 38),
    "glowstone": (171, 131, 84), "shroomlight": (240, 146, 70),
    "ochre_froglight": (250, 245, 206), "verdant_froglight": (229, 244, 228),
    "pearlescent_froglight": (245, 240, 239), "ancient_debris": (94, 66, 58),
    # metals and minerals
    "iron_block": (220, 220, 220), "gold_block": (246, 208, 61),
    "diamond_block": (98, 237, 228), "emerald_block": (42, 203, 87),
    "lapis_block": (30, 67, 140), "redstone_block": (175, 24, 5),
    "coal_block": (16, 15, 15), "netherite_block": (66, 61, 63),
    "copper_block": (192, 107, 79), "exposed_copper": (161, 125, 103),
    "weathered_copper": (108, 153, 110), "oxidized_copper": (82, 162, 132),
    "amethyst_block": (133, 97, 191), "budding_amethyst": (132, 96, 186),
    "raw_iron_block": (166, 135, 107), "raw_gold_block": (221, 169, 46),
    "raw_copper_block": (154, 105, 79), "slime": (111, 192, 91),
    "honey_block": (251, 185, 52), "honeycomb_block": (229, 148, 29),
    # plants and crops
    "short_grass": (96, 150, 52), "tallgrass": (96, 150, 52), "tall_grass": (96, 150, 52),
    "fern": (88, 140, 50), "large_fern": (88, 140, 50), "vine": (60, 110, 30),
    "seagrass": (40, 110, 60), "kelp": (60, 120, 40), "lily_pad": (32, 128, 48),
    "waterlily": (32, 128, 48), "sugar_cane": (148, 192, 101), "reeds": (148, 192, 101),
    "cactus": (85, 127, 43), "bamboo": (93, 144, 19), "pumpkin": (198, 118, 24),
    "carved_pumpkin": (198, 118, 24), "lit_pumpkin": (198, 118, 24),
    "melon_block": (114, 146, 30), "hay_block": (166, 136, 38),
    "wheat": (166, 151, 73), "carrots": (90, 140, 40), "potatoes": (90, 140, 40),
    "beetroot": (90, 130, 40), "sweet_berry_bush": (60, 100, 40),
    "dandelion": (220, 200, 40), "poppy": (190, 30, 30), "red_flower": (190, 30, 30),
    "yellow_flower": (220, 200, 40), "cornflower": (70, 100, 200),
    "sunflower": (240, 200, 30), "lilac": (180, 140, 190), "rose_bush": (150, 40, 40),
    "peony": (220, 170, 200), "dead_bush": (107, 79, 41), "deadbush": (107, 79, 41),
    "brown_mushroom_block": (149, 111, 81), "red_mushroom_block": (200, 46, 45),
    "mushroom_stem": (203, 196, 185), "azalea": (95, 120, 45),
    "flowering_azalea": (110, 120, 75), "big_dripleaf": (110, 150, 50),
    "small_dripleaf_block": (110, 150, 50), "spore_blossom": (206, 96, 158),
    "glow_lichen": (112, 131, 121), "mangrove_roots": (74, 59, 38),
    "muddy_mangrove_roots": (70, 59, 45), "crimson_roots": (126, 8, 41),
    "warped_roots": (20, 138, 124), "nether_sprouts": (19, 151, 133),
    "twisting_vines": (20, 143, 124), "weeping_vines": (104, 1, 0),
    "chorus_plant": (93, 57, 93), "chorus_flower": (151, 120, 151),
    # made things the wreck and a base are built from
    "crafting_table": (120, 73, 42), "furnace": (110, 110, 110),
    "lit_furnace": (110, 110, 110), "blast_furnace": (108, 108, 108),
    "smoker": (107, 87, 66), "barrel": (134, 100, 58), "chest": (162, 120, 50),
    "trapped_chest": (162, 120, 50), "ender_chest": (40, 55, 55),
    "bookshelf": (117, 94, 59), "chiseled_bookshelf": (117, 94, 59),
    "anvil": (68, 68, 68), "smithing_table": (57, 58, 70), "cartography_table": (103, 87, 67),
    "fletching_table": (197, 180, 133), "loom": (142, 119, 91),
    "stonecutter_block": (123, 118, 111),
    "grindstone": (142, 142, 142), "lectern": (173, 137, 83), "composter": (112, 70, 31),
    "cauldron": (74, 74, 74), "campfire": (100, 70, 40), "soul_campfire": (80, 70, 70),
    "fire": (220, 120, 30), "soul_fire": (50, 190, 200), "tnt": (182, 49, 41),
    "lantern": (106, 91, 83), "soul_lantern": (71, 99, 114), "beacon": (118, 221, 215),
    "note_block": (88, 58, 40), "jukebox": (94, 64, 47), "target": (229, 176, 168),
    "beehive": (182, 146, 94), "bee_nest": (202, 160, 74), "dried_kelp_block": (50, 58, 38),
    "bone_block": (229, 225, 207), "redstone_lamp": (95, 54, 30),
    "lit_redstone_lamp": (142, 101, 60), "observer": (98, 98, 98),
    "piston": (153, 127, 85), "sticky_piston": (153, 127, 85), "hopper": (67, 67, 67),
    "dispenser": (110, 110, 110), "dropper": (110, 110, 110), "daylight_detector": (130, 116, 94),
    "enchanting_table": (128, 50, 60), "brewing_stand": (122, 101, 81),
    "respawn_anchor": (40, 25, 55), "lodestone": (147, 149, 152), "crafter": (115, 100, 95),
    "decorated_pot": (140, 80, 60), "sponge": (195, 192, 74), "wet_sponge": (171, 181, 70),
    "spawner": (36, 46, 62), "mob_spawner": (36, 46, 62), "trial_spawner": (36, 46, 62),
    "vault": (60, 70, 80), "heavy_core": (80, 80, 90), "end_portal_frame": (91, 120, 97),
    "end_portal": (10, 15, 20), "portal": (90, 10, 190), "end_gateway": (10, 15, 20),
    "iron_door": (194, 193, 193), "iron_trapdoor": (202, 202, 202),
    "wool": (234, 236, 237), "concrete": (207, 213, 214), "carpet": (234, 236, 237),
}

# Fallbacks by what a name contains, for blocks no row names: stairs and slabs of a
# stone, an ore, a copper variant. Checked in order, so the specific come first.
_CONTAINS: tuple[tuple[str, RGB], ...] = (
    ("deepslate", (77, 77, 80)), ("blackstone", (42, 36, 41)), ("sandstone", (216, 203, 155)),
    ("prismarine", (99, 156, 151)), ("quartz", (235, 229, 222)), ("purpur", (169, 125, 169)),
    ("end_stone", (219, 222, 158)), ("nether_brick", (44, 21, 26)), ("mud_brick", (137, 103, 79)),
    ("tuff", (108, 109, 102)), ("granite", (149, 103, 85)), ("diorite", (188, 188, 188)),
    ("andesite", (136, 136, 136)), ("oxidized", (82, 162, 132)), ("weathered", (108, 153, 110)),
    ("exposed", (161, 125, 103)), ("copper", (192, 107, 79)), ("cobblestone", (127, 127, 127)),
    ("stone_brick", (122, 121, 122)), ("brick", (150, 97, 83)), ("ore", STONE),
    ("stone", STONE), ("coral", (200, 80, 120)), ("candle", (230, 220, 180)),
    ("button", CLEAR), ("pressure_plate", STONE), ("sign", (162, 130, 78)),
    ("head", (120, 120, 120)), ("skull", (120, 120, 120)), ("pot", (120, 70, 50)),
)


def _shade(rgb: RGB, by: float) -> RGB:
    if by >= 0:
        return tuple(int(c + (255 - c) * by) for c in rgb)  # type: ignore[return-value]
    return tuple(int(c * (1 + by)) for c in rgb)  # type: ignore[return-value]


def _dyed(name: str) -> RGB | None:
    for dye in _DYE_ORDER:
        if name.startswith(dye + "_"):
            family = _DYED.get(name[len(dye) + 1 :])
            if family is not None:
                swatch, lift = family
                return _shade(DYES[dye][swatch], lift)
    return None


def _wooden(name: str) -> RGB | None:
    for wood in _WOOD_ORDER:
        rest = name.removeprefix("stripped_")
        if rest.startswith(wood + "_"):
            part = rest[len(wood) + 1 :]
            if part == "leaves":
                return LEAVES.get(wood, LEAVES["oak"])
            if part in _WOODEN:
                return WOODS[wood]
    if name.endswith("_leaves"):
        return LEAVES.get(name.removesuffix("_leaves"), LEAVES["oak"])
    return None


def color(name: str) -> RGB | None:
    """The block's colour from above, `CLEAR` to look through it, None if unknown."""
    short = name.removeprefix("minecraft:")
    if short in _CLEAR or short.startswith("light_block"):
        return CLEAR
    if (rgb := BLOCKS.get(short)) is not None:
        return rgb
    if (rgb := _dyed(short) or _wooden(short)) is not None:
        return rgb
    if short.endswith("glass") or short.endswith("glass_pane"):
        return CLEAR
    for part, rgb in _CONTAINS:
        if part in short:
            return rgb
    return None
