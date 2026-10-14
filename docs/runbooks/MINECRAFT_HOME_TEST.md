# Minecraft home test — the at-home checks, in one sitting

> **Status:** Living · **Last verified:** 2026-10-13 against the code (rows added, not yet run on the box: beds and respawn points on the map, §M8 rows 29–32; /dave with cheats off; Dave's welcome lines 6 s apart; the Tricorder's equip pop-up and once-a-session hint; villagers, pets and the portal arrow, §M4e; the /dave quick menu and the arrival chime; prior: Dave's welcome and first-time beats, DW1)

The checks in `../plans/MINECRAFT_BEDROCK_PLAN.md` §M0b that need a real player on a real
client. Every in-game feature waits on them: Dave in game (M5/M6), the tricorder (M9),
Power Packs (M10), the chests (M11), the gates (M12), the laser cannon (M13) and powered
armor (M14).

Row 6 onward is **Dave in game** (M5's door, P2): the owner's three phrases, "point me home",
"mark this location as tree farm" and "navigate to tree farm", typed as `/jb:dave …`.

Rows 10–16 are **the Tricorder and the Power Pack** (M9, M10, DW1's rules), which are no
longer part of the kit: the always-installed `jbrain` packs carry them, with their icons,
into every world the box loads.

Rows 17–21 are **Dave's welcome and the first-time beats** (DW1). They happen as you play
the rows above: the welcome on your first join (rows 1–2), the Ender Pearl and Power Pack
beats at row 4, the first death at row 7. Each is once per player per world, so they are
checked where they happen; the rows only say what to look for.

Rows 29–32 are **beds and respawn points on the map** (§M8, beds): what the game really
reports for a bed, a broken bed and a respawn anchor, which the map's dashed "no bed seen
there" tile and the death card's "Respawned at …" line rest on. Play them on the test world,
then look at the map in the PWA (tap yourself; tap the death).

**The owner just plays, and the assistant reads the answers.** The test kit logs each
finding as a `[jbrain-probe]` line on the server, the `jbrain` pack logs its own as
`[jbrain] …`, and the assistant watches over the debug console
(`GET /api/debug/minecraft/logs`). There is nothing to write down. It runs on the **test
world** in slot 1, never on an imported real world.

## Before (assistant, over the debug console)

1. **Install the kit:** `POST /api/debug/minecraft/probe-pack {"install": true}`. The
   server restarts with the probe behavior pack, which only logs what it sees. Every world
   already has the rest, with no route: the `jbrain` behavior pack (the custom items
   `jbrain:power_pack` and `jbrain:tricorder`, the Power Pack recipe, the arrow)
   and the `jbrain` resource pack (their icons, pushed to every joining client), with
   `texturepacks-required=true` and `content-log-console-output-enabled=true` pinned.
2. **Check the server log for content errors** (a bad item or recipe file is logged as it
   loads). If an item failed, fix it before the owner sits down.
3. **For rows 13–16, turn the Power Pack switch on** for the test world:
   `PUT /api/debug/minecraft/worlds/slot1/power-pack {"on": true}` (log
   `[jbrain] config power_pack_keep=true`). Turn it off again afterwards.
4. **Make sure joining is open:** the allowlist is off, and the owner knows the LAN
   address (Minecraft screen → Join).

## The checks (owner, at the console or controller)

| # | Do | What it answers | Read from |
|---|---|---|---|
| 1 | **Join from Windows** (Friends → LAN Games, or Servers → add the LAN address, port 19132) | Windows reaches the server over NetherNet; the **resource pack downloads by itself** on join, with no install step and no third-party site | Minecraft screen's Players; log `Player connected` + resource pack download |
| 2 | **Join from the Xbox** (Friends → Joinable LAN Games) | Whether the Xbox sees the server in LAN Games at all (else: add it by address), and the same automatic pack download | Same |
| 3 | Open the inventory and the recipe book; search `Power` | **The items show their set-A icons and names** (not a purple-black square), and the Power Pack's recipe is in the recipe book | Your eyes |
| 4 | In survival with cheats on: `/give @s ender_pearl 8` and `/give @s blaze_powder 1`, then put the pearls in a ring round the powder in a crafting table | **The Power Pack recipe works** (eight pearls round one blaze powder) and makes one Power Pack | log `power_pack in inventory` |
| 5 | Hold **your Tricorder** (beamed into your inventory the first time you joined, named "<you>'s Tricorder") and turn around | The **actionbar arrow** follows your facing smoothly ("↗ World spawn · 120 blocks") — the M9 mechanic | log `tricorder in inventory: … lock inventory; keepOnDeath true`; your eyes on how smooth it is |
| 5b | Ask Minecraft_Dave "point my tricorder at the nearest diamonds" (it finds them, then sets the waypoint), then hold the tricorder | The arrow now leads to **Dave's waypoint** with its label, distance and depth ("↗ Diamonds · 40 blocks · y -58"), and says "You're here" on arrival | log `waypoint set for …`; your eyes |
| 6 | Type `/jb:dave what can you do` | The custom command (from the always-installed jbrain pack) appears in autocomplete and runs on a vanilla client; you see "Dave is thinking…" and then a private `<Dave> …` reply nobody else sees | log `[jbrain] {"ev":"dave",…}`; a Minecraft_Dave chat titled "In game: <your gamertag>" in the PWA |
| 6a | Stand somewhere, then `/jb:dave mark this location as tree farm` | **Saving a place** where you stand: Dave says it's saved with its coordinates | the PWA chat's `mc_place_save` step; `GET /api/debug/minecraft/logs` |
| 6b | Walk away, then `/jb:dave navigate to tree farm`, and hold the tricorder | **Navigating to it:** Dave says which way and how far; the arrow leads back to "tree farm" | log `waypoint set for …`; your eyes |
| 6c | `/jb:dave point me home` — first with no bed set, then after sleeping in a bed (or saying `/jb:dave mark this location as home`) | **Home:** with nothing to go on Dave says so and offers to mark it; with a bed the arrow points at the bed ("Home (your bed)"); a saved home wins over the bed | the reply; the PWA chat |
| 6d | `/jb:dave "Dave, mark this location as tree farm"` (with quotes), and the same without them | A **comma or a number needs quotes** (checked on a server console; the unquoted one should show a syntax error), and a leading "Dave," is ignored | your eyes |
| 7 | Die once (fall, or `/kill @s`) | **Deaths reach the travel log with their cause**, and whether a dead player's inventory is still readable (the keep-inventory work); **the Tricorder stays with you**; and a plain *Died at x, y, z in the Overworld (fell from a high place)* entry in your progress log | `GET /api/debug/minecraft/travel` deaths +1; log `player died`; ask Minecraft_Dave in the PWA for your progress log |
| 8 | Walk somewhere for a minute | Travel-log trail samples are recorded | `GET /api/debug/minecraft/travel` samples grow |
| 9 | *(optional)* Everyone at once, both devices | Memory and CPU with real players (the 2 GB cap) | debug status |
| 10 | Try to get rid of your Tricorder: the drop key (Q / the controller's drop), dragging it out of the inventory, putting it in a chest | **It's locked to your inventory** on Windows and on the Xbox: none of them work | Your eyes |
| 11 | `/clear @s jbrain:tricorder`, then `/kill @s` and respawn (or leave and rejoin) | **Re-beamed on waking:** a new one arrives with Dave's "…there you are. Without your Tricorder I nearly lost you…" line | log; your eyes |
| 12 | `/clear @s jbrain:tricorder`, then `/give @s jbrain:tricorder` (an unowned copy, which reads as retired), hold it and type `/jb:dave hello`; then `/clear` it and ask again with none at all; rejoin to get yours back | **Owner only:** no arrow (the actionbar says "This tricorder is retired"), and Dave answers "…static… that one isn't tuned to you." in chat; with none, "…kssht… no signal…". No question reaches Dave either way | your eyes; no `"ev":"dave"` line in the log |
| 13 | Switch on (Before, step 3). With a Power Pack and your Tricorder, die with a full inventory, some XP and armor | **Kept:** everything stays, one pack burns (the smallest stack), and on respawn a private "Your Power Pack burnt out…" line with a fizz | log `power pack: … kept their things` |
| 14 | Switch on. Die again with no pack (and once with a pack but someone else's Tricorder) | **Dropped like a normal death** at the spot: items, armor and off-hand fall, Curse of Vanishing items vanish, XP drops as orbs and resets, the Tricorder stays | log `power pack: … dropped their things`; check enchantments, names and durability on the dropped items |
| 15 | Switch on. Fall into the void (the End, or below bedrock in creative→survival) | The drops land **at the last place you stood**, not in the void | log; your eyes |
| 16 | Switch off. Die with a Power Pack | **Vanilla:** everything drops, the pack included; the Tricorder stays (keep on death) | your eyes |
| 17 | Your first join since the welcome shipped (everyone gets it once, even players who had joined before, owner 2026-10-12) | **The welcome, about 40 s:** the screen dark for 2 s with "…where am I?", sparkles falling onto you with a rising chime, "…what's this?", then a chime and your Tricorder in your hand (no second one if you already had yours: it's selected instead), a crackle and **SIGNAL ACQUIRED**, then Dave's six lines 6 s apart (8 s to 38 s), each readable before the next. A world with a crash site says "Look up. That ship is ours…" and "Your pod kept you alive…"; one without (an import) says "Our ship came down long ago…" and "This land has grown over…". Nobody else online sees or hears any of it | your eyes; log `[jbrain] config wreck=true` (or `false`) at load |
| 18 | Leave in the middle of a welcome (a second account, or a sibling's first join), then rejoin | **No replay and no error:** it just ends; on rejoin you have your Tricorder and no welcome | your eyes; no `welcome step … failed` line in the log |
| 19 | `/dave hello`, then `/d hello` | **The welcome's six Dave lines again**, 6 s apart, with no darkness, no title and no second Tricorder, and no "Dave is thinking…" | your eyes; no `"ev":"dave"` line in the log |
| 20 | Your first death (row 7) | Dave's two first-death lines ("…that was close…", then "The transporter can only carry you…"); a later death says nothing more. If a Power Pack saved your things on that first death (row 13 first), only the first line | your eyes |
| 21 | Your first Ender Pearl, then your first Power Pack (row 4) | The pearl line once ("…is that an Ender Pearl?…"); on the first Power Pack, the field line only with the keep switch on, and the saved-places line only with the transport switch on. With both off, nothing, and it comes the next time you hold a pack after one is turned on | your eyes |
| 22 | Type `/dave` with nothing after it, then close the chat | **The quick menu** opens once chat is closed (a form can't show over the open chat; the pack asks again each second for 10 s, then prints the old "Ask Dave something…" hint): *Where am I?*, *Point me home*, *My last death*, *Save this spot* (a "Name this spot" box) and *Ask something else…* (an "Ask Dave" box). Each asks Dave as if typed; closing a form does nothing. On a controller too | your eyes; log `[jbrain] {"ev":"dave",…}` with the button's words |
| 23 | Have Dave point your Tricorder somewhere close (row 5b), walk there holding it; then again with it in your pocket, not in hand | **The arrival chime:** one quiet XP-orb ding as "You're here" shows, not repeated while you stand there; carried in a pocket, the ding comes when the map's arrival clear lands (within about 10 s). Nobody else hears it, and nothing during a live scan | your ears |
| 24 | Tame a wolf and a horse, name the wolf with a name tag, make it sit; then `/dave where's my dog` | **Dave finds it:** "*Rex* is *N* blocks *…*, sitting right where you left him. Hold your Tricorder and follow the arrow.", free within 48 blocks, a deep scan farther; the Tricorder points at it | `GET /api/debug/minecraft/map/pets?x=0&z=0&owner_name=<you>` lists both, `owner` set (the `pid` on your `dave` line in `GET /minecraft/logs`) |
| 25 | Open a librarian's trade screen; then `/dave who sells <one of its books>` | Only the trades the screen shows (its unlocked levels); the line "From up here I see a librarian … who sells … for N emeralds." | `GET /api/debug/minecraft/map/villagers?x=<x>&z=<z>&profession=librarian` matches the screen |
| 26 | Light a Nether portal, go through, ask Dave to point you home | In the Nether the held Tricorder reads `↑ Nether portal · N blocks · Home is in the Overworld`, pointing at the portal; back through, it points home | `GET /api/debug/minecraft/map/portals` lists the portal; break one and `check=false` still lists it while the default leaves it out |
| 27 | Rejoin, select your Tricorder, wait, then switch to another slot and back to it a few times | **The heading isn't hidden:** the first hold this session shows "Your Tricorder points where Dave says · ask with /dave" on the actionbar for about 3 s, then the heading; it doesn't come back on a re-select (only after leaving and rejoining). The held-item pop-up over the hotbar shows just "<you>'s Tricorder", one line, no description, and the heading ("↑ World spawn · N blocks") is readable within about 2 s of each re-select. A Tricorder from before this fix loses its old "Points where Dave says" line within a second | your eyes |
| 28 | On a world with **cheats off** (the slot's allow-cheats off in the PWA), as a non-operator if you can: `/dave where am I` | **Dave answers**, with no "cheats must be enabled" error; `/d` and `/ai` work too | log `"ev":"dave"` line |
| 29 | Place a bed and sleep in it (or tap it); note the bed's block coordinates | **Which block `getSpawnPoint()` names** (the bed's own, the other half, or the spot beside it): the pack counts a bed at or touching the point, and the map's **Bed** line and **Show the bed** tile sit on it. A join always resends the line | `GET /api/debug/minecraft/logs`: `[jbrain] {"ev":"spawnpoint","player":…,"kind":"bed",…}` a tick after; its x/y/z against the bed's; the PWA map, tap yourself |
| 30 | Standing near it, break that bed and wait 10 s; then die (`/kill @s`) and respawn | **A broken bed before the next death:** the 10 s check reads `kind` `other` at the same point (the game keeps the point), so the map draws the bed **dashed**, *no bed seen there*; after the respawn the line is `none` and the death card says only *Respawned at the world spawn* | log `"kind":"other"` then `"kind":"none"`; the PWA map: tap yourself, then the death |
| 31 | In the Nether, set a respawn anchor with glowstone and use it; then die until its charges run out | **An anchor:** `kind` `anchor` with `"dim":"nether"`; and **what an anchor at zero charges reports** (still `anchor`, or `none` once the game gives up on it) — record which | log `"kind":"anchor"` lines; the PWA's Layers → In this world |
| 32 | With the bed back, die somewhere away from it and respawn; and once with no bed set | **How far a waking lands:** the death card says *Respawned at their bed* only within 4 blocks of the point, and *at the world spawn* within 16 of it (the box's `spawnRadius` is 10) — record the real distances; the curve ends at the bed or the star | log `"ev":"respawn"` x/z against the bed's and the world spawn's; the PWA map, tap the death |

## After (assistant)

1. **Record every answer** in the plan's M0b section and §3c's compatibility table, and
   unblock or re-plan the waves they gate.
2. **Remove the kit:** `POST /api/debug/minecraft/probe-pack {"install": false}`. The
   server restarts without the probe pack; the `jbrain` packs stay, as in every world.
3. **Turn the Power Pack switch back off** unless the owner wants it:
   `PUT /api/debug/minecraft/worlds/slot1/power-pack {"on": false}`.

## If something fails

- **No pack download prompt, items show as purple-black squares:** the client didn't take
  the resource pack. Check that `texturepacks-required` is set and the pack is listed in
  `world_resource_packs.json`. If the pack really can't be pushed to that client, M9/M10
  fall back to the no-download designs in the plan.
- **An item is missing entirely:** a content error, which is in the log from step 2 of
  "Before". Usually the item's `format_version` or a component name.
- **The Xbox can't see the server under LAN Games:** add it as a server by the LAN address
  and port 19132. Note which one worked; it decides the join instructions for the family.
