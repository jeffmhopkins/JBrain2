# Minecraft home test — the at-home checks, in one sitting

> **Status:** Living · **Last verified:** 2026-10-10

The checks in `../plans/MINECRAFT_BEDROCK_PLAN.md` §M0b that need a real player on a real
client. Every in-game feature waits on them: Dave in game (M5/M6), the tricorder (M9),
Power Packs (M10), the chests (M11), the gates (M12), the laser cannon (M13) and powered
armor (M14).

**The owner just plays, and the assistant reads the answers.** The test kit logs each
finding as a `[jbrain-probe]` line on the server, and the assistant watches over the debug
console (`GET /api/debug/minecraft/logs`). There is nothing to write down. It runs on the
**test world** in slot 1, never on an imported real world.

## Before (assistant, over the debug console)

1. **Install the kit:** `POST /api/debug/minecraft/probe-pack {"install": true}`. The
   server restarts with:
   - the probe behavior pack: the custom items `jbrain:power_pack` and
     `jbrain:tricorder`, and the nine-Eye-of-Ender recipe;
   - the probe resource pack (their icons), which the server now pushes to every joining
     client;
   - `texturepacks-required=true` and `content-log-console-output-enabled=true`.
2. **Check the server log for content errors** (a bad item or recipe file is logged as it
   loads). If an item failed, fix it before the owner sits down.
3. **Make sure joining is open:** the allowlist is off, and the owner knows the LAN
   address (Minecraft screen → Join).

## The checks (owner, at the console or controller)

| # | Do | What it answers | Read from |
|---|---|---|---|
| 1 | **Join from Windows** (Friends → LAN Games, or Servers → add the LAN address, port 19132) | Windows reaches the server over NetherNet; the **resource pack downloads by itself** on join, with no install step and no third-party site | Minecraft screen's Players; log `Player connected` + resource pack download |
| 2 | **Join from the Xbox** (Friends → Joinable LAN Games) | Whether the Xbox sees the server in LAN Games at all (else: add it by address), and the same automatic pack download | Same |
| 3 | Open the inventory and the creative or recipe search; type `Power` | **The custom items show their own icons and names** (not a purple-black square) | Your eyes |
| 4 | In survival with cheats on: `/give @s ender_eye 9`, then put all nine in a crafting table | **The nine-eye recipe works** and makes one Power Pack | log `power_pack in inventory` |
| 5 | `/give @s jbrain:tricorder`, then hold it and turn around | The tricorder's **actionbar arrow** follows your facing smoothly ("↗ World spawn · 120 blocks") — the M9 mechanic | log `tricorder held`; your eyes on how smooth it is |
| 6 | Type `/jb:dave hello` | The custom command appears in autocomplete and runs on a vanilla client | log `command from …` |
| 7 | Die once (fall, or `/kill @s`) | **Deaths reach the travel log with their cause**, and whether a dead player's inventory is still readable (the keep-inventory work) | `GET /api/debug/minecraft/travel` deaths +1; log `player died` |
| 8 | Walk somewhere for a minute | Travel-log trail samples are recorded | `GET /api/debug/minecraft/travel` samples grow |
| 9 | *(optional)* Everyone at once, both devices | Memory and CPU with real players (the 2 GB cap) | debug status |

## After (assistant)

1. **Record every answer** in the plan's M0b section and §3c's compatibility table, and
   unblock or re-plan the waves they gate.
2. **Remove the kit:** `POST /api/debug/minecraft/probe-pack {"install": false}`. The
   server restarts without the probe packs, and the two test properties are cleared.

## If something fails

- **No pack download prompt, items show as purple-black squares:** the client didn't take
  the resource pack. Check that `texturepacks-required` is set and the pack is listed in
  `world_resource_packs.json`. If the pack really can't be pushed to that client, M9/M10
  fall back to the no-download designs in the plan.
- **An item is missing entirely:** a content error, which is in the log from step 2 of
  "Before". Usually the item's `format_version` or a component name.
- **The Xbox can't see the server under LAN Games:** add it as a server by the LAN address
  and port 19132. Note which one worked; it decides the join instructions for the family.
