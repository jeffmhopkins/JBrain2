# jbrain-pack — the box's own behavior pack

The behavior pack that carries the crew's story into every world the box loads. The
wrapper (`../server.py`, `Rig.install_pack`) copies it into the loaded world and lists it
in `world_behavior_packs.json` on **every start**, so a world always runs the version this
image ships. Its icons and block faces are the matching resource pack,
[`../jbrain-rp/`](../jbrain-rp/README.md).

It uses only the **stable** `@minecraft/server` and `@minecraft/server-ui` 2.0.0 APIs, so
it needs no experiments and runs on vanilla clients (plan §3c).

- **The design:** `docs/plans/MINECRAFT_BEDROCK_PLAN.md`. The `§` in each file's header
  comment points to the section it implements.
- **The canon:** Bleepbloop_City's
  [`notes/lore.md`](https://github.com/jeffmhopkins/Bleepbloop_City/blob/main/notes/lore.md)
  decides what each item does and every word Dave says.

## What it adds to the game

| Feature | Plan | In the game |
| --- | --- | --- |
| **`/dave`** (also `/d`, `/ai`, and `/jb:dave`) | §M5, §M7 | The in-game door to Dave. `/dave <words>` sends the question to the backend as a `dave` event line, and the answer comes back as chat. `/dave` on its own opens a quick menu: Where am I?, Point me home, My last death, Save this spot, Ask something else. Up to eight words need no quotes. Commas or numbers do: `/dave "mark 100 64 -20 as base"`. |
| **The Tricorder** (`jbrain:tricorder`) | §M9, §DW1 | Beamed to each player in the welcome, bound to them (owner and serial on the item), locked to the inventory and kept on death. Held, it shows an arrow and distance to the waypoint Dave set, and chimes on arrival. A lost one is re-beamed and the old one retired. Without your own Tricorder, Dave can't hear you. |
| **The Tricorder's live scan** | §M25 | "Dave, set my Tricorder to scan for diamonds": every 10 s it finds the nearest such block within 16 blocks and the arrow points at it. Kept per player, so it survives a logout. |
| **The Power Pack** (`jbrain:power_pack`) | §M10 | Crafted from 8 Ender Pearls around Blaze Powder. Carried with your own Tricorder when you die, one pack burns and you keep everything. This only applies when the world's charm switch is on. A void death's drops land where you last stood safely. |
| **Transport on request** | §M10 | "Dave, take me to the tree farm": one of your own saved places, a Yes/No form, and on Yes one Power Pack burns and you're teleported. The world has a switch for this. |
| **Wormhole gates** (`jbrain:wormhole_seed`) | §M12 | A Wormhole Seed planted inside a 4 × 5 ring of diamond blocks makes a dormant gate. Dave opens it or re-aims it at one of its planter's saved places. Anyone who steps through goes there, one way. |
| **The Replicator** (`jbrain:replicator`) | §M15 | 8 Power Packs around a furnace. Dave programs it to make one thing. It burns blocks from a container on top into that thing, takes fuel from a container beside it, and outputs into a container below, like a furnace with hoppers. |
| **The No-Spawn Beacon** (`jbrain:no_spawn_beacon`) | §M16 | 8 echo shards around a Power Pack. Placed, it keeps the 3 × 3 chunks around it (48 × 48 blocks, full height) clear of wild mobs: hostile, neutral and passive. Mobs are beamed away (sparks and a puff), never killed, so nothing drops. Mobs that are tamed, named, leashed, bred or interacted with stay. So do villagers, golems and bosses. |
| **Dave's item guide** | §M19 | Dave explains each item the first time you hold its key ingredient, the way the recipe book unlocks. Ender Pearl → Power Pack; first Power Pack → its uses; echo shard or popped chorus fruit (after a Power Pack) → the Beacon or the Wormhole Seed. |
| **Dave's welcome and first-time beats** | §DW1 | A scripted welcome on a player's first join to a world, plus one-time beats (first Ender Pearl, first Power Pack, first death). Each beat plays once per player per world. Lines play 6 s apart. These are scripted, never an AI turn: instant, the same for everyone, and safe for kids. |
| **The travel log's deaths and beds** | §T1, §M8 | Deaths and respawns (which BDS never prints) and each player's respawn point, for the map. |

## The files

Each feature is split in two. A `*_rules.js` file holds the pure logic and imports nothing
from `@minecraft/*`, so node can test it directly. Its partner reads and writes the world.

| File | Role |
| --- | --- |
| `scripts/main.js` | The entry point (`manifest.json`'s script module). Holds the Tricorder, the Power Pack, deaths, the item guide, the arrival chime, `/dave` and its menu, and the inbound scriptevents. Calls each feature's `init*`. |
| `scripts/rules.js` | `LINES` (**Dave's words, verbatim from the lore page**, signed off by the owner), the arrow, Tricorder ownership, the death table, unlocks, and the parsers for each scriptevent. |
| `scripts/welcome.js` / `welcome_rules.js` | Dave's welcome and the first-time beats, and their timings. |
| `scripts/scan.js` | The Tricorder's live scan (its rules are in `rules.js`). |
| `scripts/transport.js` / `transport_rules.js` | Transport on request. |
| `scripts/wormhole.js` / `wormhole_rules.js` | Wormhole gates: ring detection, records, opening, re-aiming, collapse. |
| `scripts/replicator.js` / `replicator_rules.js` | The Replicator's script-run furnace and its panel. |
| `scripts/beacon.js` / `beacon_rules.js` | The No-Spawn Beacon's coverage and sweep. |
| `scripts/spawnpoint.js` / `spawnpoint_rules.js` | Each player's respawn point and what kind it is (bed, anchor, gone). |
| `items/*.json` | The custom items: Tricorder, Power Pack, Wormhole Seed, and the Replicator's two placer items (plain and programmed, so the slot can show the lit sprite). |
| `blocks/*.json` | The custom blocks: Replicator, No-Spawn Beacon, and the wormhole surface. |
| `recipes/*.json` | Power Pack, Replicator, No-Spawn Beacon, Wormhole Seed. Each unlocks in the recipe book from its key ingredient. |
| `loot_tables/blocks/replicator.json` | What a broken Replicator drops. |

World state lives in **dynamic properties**, so the pack needs no backend to remember
things. Examples: each player's Tricorder serial, waypoint, scan, and unlocks; each gate's
record; each beacon. Each property is keyed with a `jb:` prefix.

## Talking to the wrapper and to Dave

The console is the only channel (see [`../README.md`](../README.md#how-it-fits-together)).

**Out:** `[jbrain] {"ev": …}` lines. `../bds.py` accepts only the known `ev` kinds and
type-checks every field, because the text is player-influenced:

| `ev` | When |
| --- | --- |
| `death`, `respawn` | A player dies or respawns (the travel log). |
| `spawnpoint` | A player's respawn point changes, and on every join. |
| `dave` | A `/dave` question, with where the asker stands. |
| `scan` / `scans` | A scan is set, finds something, or stops / every kept scan, after each load. |
| `gate` / `gates` | A gate is planted, opened, aimed, goes dormant, or collapses / every gate, after each load. |
| `beacon` / `beacons` | A beacon is placed or broken / every beacon, after each load. |

Plain `[jbrain] <text>` lines are for people reading the log. Transport's outcome lines
are `[jbrain] transport <id> <outcome> {json}`, which Dave's `mc_transport` reads back.

**In:** `scriptevent jb:<name> {json}`, sent by the wrapper or by Dave's tools:

| Event | From | Does |
| --- | --- | --- |
| `jb:config` | wrapper, on each start | The world's switches: the Power Pack charm, transport, whether a crash site was built. |
| `jb:waypoint` | Dave | Points a player's Tricorder somewhere (kept while they're offline). |
| `jb:scan` | `mc_tricorder_scan` | Sets or stops a player's live scan. |
| `jb:find` | Dave | Nearest mob of a kind near players (only the game knows). |
| `jb:items` | Dave | Item stacks lying on the ground near a player (a death's drops). |
| `jb:carry` | Dave | What the asker carries toward a goal (their own inventory only). |
| `jb:transport` | `mc_transport` | A transport offer to one of the asker's saved places. |
| `jb:wormhole` | Dave | Opens or re-aims a gate. |
| `jb:replicator` | Dave | Programs a Replicator. |

The backend side of each is under `backend/src/jbrain/agent/minecraft*.py` and
`backend/src/jbrain/minecraft/`.

## Testing

The node tests are in [`../jbrain-sim/`](../jbrain-sim/README.md). They run under the
supervisor's pytest, which copies this pack beside a fake `@minecraft/server`:

```sh
(cd supervisor && uv run pytest tests/test_minecraft_jbrain_pack.py \
  tests/test_minecraft_replicator.py tests/test_minecraft_wormhole.py \
  tests/test_minecraft_beacon.py tests/test_minecraft_transport_switch.py)
```

Those Python tests also pin the JSON: item and block definitions, recipes, and that every
texture they name exists in `../jbrain-rp/`. What only a real client can show (icons,
forms, sounds, how a teleport feels) is in `docs/runbooks/MINECRAFT_HOME_TEST.md`.

## Changing it

- **Dave's words:** change the lore page first, then `LINES` in `scripts/rules.js`,
  verbatim. A test checks the lines appear only in that block.
- **Stable APIs only.** Anything beta or experimental would stop the pack loading on the
  family's vanilla worlds.
- **Logic goes in a `*_rules.js` file** with its test; the world-facing file stays thin.
- **A new item or block** needs its icon or faces in `../jbrain-rp/` in the same PR.
  Otherwise players see a missing-texture square.
- **A new `ev` kind** must be added to `PACK_EVENTS` in `../bds.py`, or the wrapper drops it.
- **When `jbrain-rp`'s version changes**, update this manifest's dependency on it to
  match. `test_minecraft_jbrain_pack.py` checks the two agree.
