# minecraft — the family Bedrock server and the world Dave can see

The `minecraft` sidecar: Mojang's Bedrock Dedicated Server (BDS) plus a small stdlib
Python wrapper that owns its console, its worlds and backups, and everything the rest of
JBrain reads out of the world — map tiles, chests, villagers, pets, structures. It also
carries the behavior and resource packs that put the crew's story into every world: the
Tricorder, Power Packs, wormhole gates, the Replicator, the No-Spawn Beacon, the spawn
wreck and Dave's welcome.

- **The design and every wave** is `docs/plans/MINECRAFT_BEDROCK_PLAN.md`. Section
  numbers in this folder's comments (`§M8`, `§SW1`, `§DW1`, …) point into it.
- **The story** — the Old Makers, Dave, the crew, the Tricorder, and Dave's words — is
  canon in the Bleepbloop_City repo's
  [`notes/lore.md`](https://github.com/jeffmhopkins/Bleepbloop_City/blob/main/notes/lore.md).
  When the code here and the lore page disagree, the lore page wins.
- **The at-home checks** on a real client are `docs/runbooks/MINECRAFT_HOME_TEST.md`.

## How it fits together

```
 players (Windows, Xbox, phones)
        │  19132/udp (NetherNet), host network
        ▼
 ┌─────────────────── minecraft container ───────────────────┐
 │  BDS (in the /data volume) ◄── stdin / stdout ──► bds.py  │
 │    │ loads the world's packs                       │      │
 │    ▼                                               ▼      │
 │  jbrain-pack + jbrain-rp           server.py (HTTP API)   │
 │  wreck-pack (new worlds only)      worlds, map, indexes   │
 └────────────────────────────────────────────▲──────────────┘
                                              │ MC_TOKEN bearer, MC_PORT
 api (backend/src/jbrain/minecraft/client.py) ┘
   ├─ the Minecraft Ops card and the map in the PWA (api/minecraft.py)
   ├─ Dave's tools (agent/minecraft*.py) and the /dave door (minecraft/door.py)
   └─ the debug console (api/debug_minecraft.py, docs/runbooks/DEBUG_ACCESS.md)
```

- **BDS lives in the volume, not the image.** Bedrock clients auto-update and only join a
  server on their exact version, so the server must follow them on a container restart,
  not an image rebuild. `install.py` downloads and unpacks it into `/data/server` and an
  update never touches the world, the allowlist or the permission files.
- **The console is the only control channel.** BDS has no RCON; `bds.py` owns its stdin
  and stdout and nothing else writes to them. Graceful stop, consistent hot backups
  (`save hold` / `save query` / `save resume`), `locate`, and every `scriptevent` the
  backend sends to the packs go through it.
- **The packs talk back on the console.** The packs' scripts print `[jbrain] {json}`
  lines (deaths, respawns, spawn points, `/dave` asks, scans, gates, beacons, the wreck's
  progress). `bds.py` turns those into events, which the api polls from `GET /events`
  (`backend/src/jbrain/minecraft/sessions.py`, `door.py`).
  The backend talks the other way with `scriptevent jb:<name> {json}` (waypoints, scans,
  transport offers, gate aims, Replicator programs, the world's `jb:config`).
- **The world's files are read directly, read-only.** `leveldb.py` reads the world's
  LevelDB without taking its lock, so the map, chest search and villager lookups work
  while the server runs (and while it's stopped).
- **Packs are copied into the world on every start** (`Rig.install_pack`), so a world
  always runs the version this image ships. Each pack's `README.md` is left out of the
  copy, because a world's packs travel in its `.mcworld` export and the resource pack is
  pushed to every client.
- **It runs on the host network.** NetherNet needs the box's real address and LAN
  broadcast, so the control port is reachable from the LAN: every route but `/healthz`
  needs the `MC_TOKEN` bearer, and with no token configured they all refuse.

## The files

### The wrapper (Python, stdlib only)

| File | What it is |
| --- | --- |
| `server.py` | The sidecar's entry point: the HTTP control surface (`http.server`) and the `Rig` that runs lifecycle, world slots, backups, updates, pack installs, the travel log and the structure sweep. |
| `bds.py` | The running BDS process: start/stop, the console pipe, per-command replies, the player list from the log, hot snapshots. |
| `install.py` | Fetch, unpack and configure BDS in the data volume; the pinned `server.properties` defaults. |
| `worlds.py` | World slots (one loaded at a time; slot 1's folder is `world`) and the backup index (which slot, automatic vs the owner's, pinned, last downloaded). |
| `leveldb.py` | A read-only reader for Mojang's LevelDB fork (zlib instead of Snappy). |
| `nbt.py` | Little-endian NBT, bounds-checked for untrusted input. |
| `mapping.py` | Map tiles: the biome atlas and the true-colour surface layer, with unexplored ground predicted from the seed via `jbrain-predict`. Bedrock saves only chunks something changed in, so ground players merely crossed is missing from the files; the api sends that ground with each tile (from the travel log), and it is drawn as the prediction at full strength. |
| `blockcolors.py` | What colour each block looks from above, for the surface layer. |
| `blocks.py` | "Where's the nearest diamond?" — block search over explored chunks. |
| `containers.py` | "Which chest has my diamonds?" — chests, barrels, shulkers, frames, pots, lecterns. Ender chests are never read. |
| `entities.py` | Villagers and their current trades, pets, named mobs, and portals. |
| `structure_sweep.py` | The map's Structures overlay: a grid of `locate structure` calls, saved per world. |
| `structures.py` | Image-build step: turns `wreck/*.json.gz` into `.mcstructure` files inside `wreck-pack/`. |

### The packs and their tests

| Folder | What it is |
| --- | --- |
| [`jbrain-pack/`](jbrain-pack/README.md) | The box's own **behavior pack**, installed into every world on every start: the `/dave` door, the Tricorder, Power Packs, transport, wormhole gates, the Replicator, the No-Spawn Beacon, Dave's welcome, and the event lines above. |
| [`jbrain-rp/`](jbrain-rp/README.md) | Its **resource pack**: the custom items' icons and blocks' faces, pushed to every player on join. |
| [`jbrain-sim/`](jbrain-sim/README.md) | Node tests for `jbrain-pack`, against a fake `@minecraft/server`. |
| [`wreck-pack/`](wreck-pack/README.md) | The **spawn wreck**: builds the crashed escape pod and the mothership at a new world's spawn, once. Only installed into worlds the box generated itself. |
| `wreck/` | The wreck's builds as compact block lists, exported by `spikes/minecraft-spawn-wreck/export.py`. |
| `wreck-sim/` | Node tests for `wreck-pack`: its pure planning, and the real `main.js` against synthetic worlds. See [`wreck-pack/README.md`](wreck-pack/README.md#testing). |
| [`probe-pack/`](probe-pack/README.md) | The home-test kit: a pack that only logs what it sees, installed on request over the debug API. |
| [`predict/`](predict/README.md) | `jbrain-predict`, the one compiled piece: a world's surface predicted from its seed (the map's "satellite" layer). |

## The HTTP surface

Bearer-guarded JSON, on `MC_PORT` (compose sets `MC_CONTROL_PORT`, default 19180). The
api is the only caller; the owner reaches all of it through the PWA, and the assistant
through the debug console. Routes, by area:

- **Health and server:** `GET /healthz` (unauthenticated, the container's healthcheck),
  `GET /status`, `GET /logs`, `GET /version`, `POST /server/{start,stop,restart}`,
  `POST /update`, `POST /command` (the console, minus what has a safer route).
- **Settings:** `GET/POST /properties` (`server.properties` overrides; the ports are
  locked), `GET/POST /settings`, `GET/POST /allowlist`.
- **Worlds:** `GET /worlds`, `GET /worlds/new-seed`,
  `POST /worlds/slotN/{load,create,update,reset}`, `POST /worlds/slotN/import`
  (a streamed `.mcworld` upload), `POST /worlds/slotN/rules`,
  `POST /worlds/slotN/power-pack`, `POST /worlds/slotN/power-pack-transport`.
- **Backups:** `GET /snapshots`, `POST /snapshot`, `GET /snapshots/<name>.mcworld/file`
  (download), `POST /snapshots/<name>.mcworld/{pin,delete,restore}`.
- **Map and world index:** `GET /map/info`, `GET /map/tile/<dim>/<zoom>/<x>/<z>.png`
  (`?visited=` is the api's bitmap of the tile's crossed chunks),
  `GET /map/find`, `/map/items`, `/map/stock`, `/map/entities`, `/map/villagers`,
  `/map/pets`, `/map/portals`, `/map/structures`, `/map/point`, `/map/nearby`,
  `POST /map/structures/sweep`.
- **Play:** `GET /track` (the travel log), `GET /events` (the packs' event lines).
- **Test kit:** `POST /probe-pack {"install": true|false}`.

A world operation that runs past about 20 s answers `202` and carries on as a job, because
Cloudflare's tunnel cuts a request at about 100 s and a big world's load or restore can take
longer.

## Configuration

Set in `deploy/docker-compose.yml` from the box's `.env`; the defaults suit the LAN test.

| Variable | Default | What it does |
| --- | --- | --- |
| `MC_TOKEN` (compose: `MINECRAFT_TOKEN`) | empty | The bearer the api sends. Empty means every route but `/healthz` refuses. |
| `MC_PORT` (compose: `MC_CONTROL_PORT`) | `8000` (compose `19180`) | The control port. |
| `MC_DATA_DIR` | `/data` | The volume: `server/` (BDS and its worlds), `snapshots/`, `structures/`, the slot and backup indexes. |
| `MC_BDS_VERSION` | `latest` | Which BDS to install; `latest` follows Mojang. |
| `MC_SLOTS` | `5` | How many world slots. |
| `MC_SERVER_NAME`, `MC_LEVEL_NAME`, `MC_LEVEL_SEED`, `MC_GAMEMODE`, `MC_DIFFICULTY` | `JBrain`, `world`, random, `survival`, `normal` | The first world's starting `server.properties`. Per-world settings after that live on the slot. |
| `MC_ALLOW_LIST` | `false` | Off for LAN play, so an empty allowlist can't lock the family out. |
| `MC_TRANSPORT` | `nethernet` | Bedrock's network transport. |
| `MC_PREDICT_BIN` | `/app/jbrain-predict` | Where the seed predictor is. Without it the map draws explored chunks only. |

The owner changes all of this day to day from the PWA's Minecraft Ops card, not from
`.env` (CLAUDE.md #10).

## The image

`deploy/Dockerfile.minecraft` builds `jbrain-predict` against a pinned cubiomes commit in its
own stage, then copies the wrapper and the packs into a `debian:trixie-slim` with
Debian's `python3` and nothing from pip. At build time it:

- runs `structures.py build wreck wreck-pack` to make the wreck's structure files;
- imports every module, as a smoke test;
- checks the predictor against spots a real 1.26 server generated from a known seed (a
  warm ocean, a mangrove swamp, a warped forest, an End island and the void beside it).

The process runs as the unprivileged `mc` user (uid 1000). The healthcheck tests the
wrapper, not the game, so a slow first-boot download doesn't restart the container.

The container is not egress-free: BDS verifies every player's Xbox Live sign-in, and the
wrapper downloads server updates.

## Testing

The Python here is **linted by nothing but typechecked and tested by `supervisor`**:
`supervisor/pyproject.toml` puts `../deploy/minecraft` in pyright's `include`, and its
tests load the wrapper by path (`supervisor/tests/test_minecraft_*.py`). The pack tests
also run under that pytest, which lays each pack out beside a fake `@minecraft/server`
and runs `node --test` (node comes from `scripts/dev-setup.sh`). Run the whole
`supervisor` gate, as CI does:

```sh
(cd supervisor && uv run ruff check . && uv run ruff format --check . \
  && uv run pyright && uv run pytest)
```

Don't run bare `ruff format` over `deploy/minecraft/`: it isn't in any ruff config, and
the default width would reformat the files.

- `test_minecraft_sidecar.py` runs the wrapper against a fake BDS (a Python script that
  speaks the same console lines): replies per command, players from the log, consistent
  snapshots, updates that never overwrite the world.
- `test_minecraft_mapping.py`, `_containers.py`, `_entities.py` read real LevelDB files
  written on disk, and real entity records captured from BDS 1.26.52.3.
- `test_minecraft_jbrain_pack.py`, `_replicator.py`, `_wormhole.py`, `_beacon.py`,
  `_transport_switch.py` cover `jbrain-pack`; `test_minecraft_wreck.py` covers the wreck.

CI also builds `deploy/Dockerfile.minecraft` whenever anything here changes, so the image's
build-time checks run on every PR.

`scripts/dev-setup.sh` builds `jbrain-predict` into `~/.cache/jbrain/` for rendering real
predicted tiles on a workstation (`MC_PREDICT_BIN=~/.cache/jbrain/jbrain-predict`); the
tests use a fake, so it isn't needed for them.

## Changing things here

- **Dave's words** change on the lore page first, then in `jbrain-pack/scripts/rules.js`
  (`LINES`), verbatim.
- **A new pack file** gets a test in the same PR (CLAUDE.md #5), and a new console event
  line also needs its handling in `bds.py` (`_pack_event`) and its reader in the backend.
- **A new dependency or setup step** goes into `scripts/dev-setup.sh` in the same PR
  (CLAUDE.md #8). The wrapper stays stdlib only.
- **A new operator action** must work from the PWA or the debug API, never only from a
  shell (CLAUDE.md #10).
