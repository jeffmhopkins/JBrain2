# Minecraft Bedrock — an on-box world server, its backups, and a companion that knows the world

> **Status:** Proposed · **Last verified:** 2026-10-10 · **Waves:** M0◻️ M1◻️ M2◻️ M3◻️ M4◻️ M5◻️ M6◻️ M7◻️

The owner wants a Minecraft **Bedrock** dedicated server on the box. They need to start and
stop it, back up its world, and **import an existing world** they already play. On top of
that, they want a **chat companion** that players talk to from inside the game and that
answers from the world's real data: "where's the nearest pig?", "where's the nearest woodland
mansion?", "where did I die?".

This doc records the idea in phases. Nothing is built. The phases are ordered so that each one
is useful by itself. M1–M3 give a server the owner runs and backs up from the PWA, with no
companion at all. The companion (M4–M6) is built on top of the snapshot and console seams
those phases create.

Reconciled with the root `CLAUDE.md` non-negotiables up front, because each one shapes the
design:

- **#1** — the companion's model calls go through the `LlmRouter`. The game sidecar never calls
  a provider.
- **#2** — world uploads, snapshots and exports are written by the backend through the storage
  abstraction (`BlobStore` / `BackupShelf`). The sidecar streams bytes over HTTP and never
  writes into JBrain's storage paths.
- **#3** — every new table is owner-RLS'd and has an isolation test.
- **#5** — tests travel with each wave. The LLM is faked. The sidecar is tested against a fake
  BDS process.
- **#8** — the new image and any parser dependency land in `scripts/dev-setup.sh` in the same
  PR.
- **#10** — the owner has **no terminal**. Every operator action in this plan is either a PWA
  control or a debug-API route, and §7 names every place that still needs host access.
- **#11** — the sidecar's Python lives under `deploy/minecraft/`. It must be added to one
  package's ruff, pyright and pytest configuration (the `deploy/sdr` precedent is
  `supervisor`), so that CI actually checks it.

## 1. What already exists to build on

The patterns are mapped from the current tree (2026-10-10). Each sub-system copies a shipped
precedent; none of them is new.

| Need | Precedent to copy |
|---|---|
| Optional container behind a compose profile | `sdr` / `comfyui` / `jcode` profiles in `deploy/docker-compose.yml`. Every service needs `logging: *logbound`, which `test_compose_logging.py` enforces. |
| Start/stop from the PWA | Supervisor `/start` and `/stop` act on an existing container. The backend proxies them through `api/ops.py` (`_lifecycle`), and `OpsScreen.tsx` renders the `ServiceRow`. The container must first be **created** by an update with the profile on, as comfyui is. |
| Turning on a profile without a terminal | `update-inner.sh` appends a flag to `.env` and adds `--profile` (the SDR block). On-box models are the other model: the PWA queues the request in the settings store, and the update or `/provision` one-shot acts on it (`local-models-sync.sh`). |
| Sidecar the backend talks to directly | SDR: a dedicated network shared only with `api`, plus `settings.sdr_url`. It is hardened like `pysandbox` (`cap_drop: [ALL]`, `no-new-privileges`, `mem_limit`). |
| Draining sidecar events into Postgres | `sdr/aprslog.py` drains `/packets` into `app.aprs_packets`. It is started in `main.py` and pruned in `worker.py`. Its text is treated as untrusted. |
| Agent tools that disappear when the feature is off | `OPTIONAL_SDR_TOOLS` in `readtools.py`, `.tool` sidecars, and the `toolSummary.ts` entries that `test_tool_step_polish.py` gates. |
| Scheduled jobs in-app (not cron) | The Phase 5 workflow scheduler (`workflow/scheduler.py`), exposed at `/ops/automations` and `/ops/schedules`. |
| Owner operator actions without a terminal | The debug router (`api/debug.py`). New routes get a row in the `DEBUG_ACCESS.md` route table. |
| Published non-HTTP port | Only the wall's `${BRAIN_HOST_BIND}:8800` is published today. Bedrock follows that shape: `${MC_BIND:-0.0.0.0}:19132:19132/udp`. |

Host fit: the Strix Halo box is **x86_64**. That matters, because Bedrock Dedicated Server
(BDS) ships only for x86_64 Linux and Windows. Memory is the tight resource, since resident
model weights dominate RAM. The container gets a hard `mem_limit`. A start would be 2–3 GB for
a handful of players; M0 measures the real figure.

## 2. Shape of the system

```
 Bedrock clients ──UDP 19132──►  minecraft sidecar (profile: minecraft)
                                 ├─ BDS process (Mojang binary, run by the wrapper)
                                 ├─ wrapper (Python): owns BDS stdin/stdout, graceful stop,
                                 │    snapshot (save hold/query/resume), allowlisted console,
                                 │    world import, LevelDB index export, companion bridge
                                 └─ volume jbrain_minecraft (worlds/, config, BDS binary)
                                        ▲  HTTP on the `minecraft` network (api only)
 api (backend) ─────────────────────────┘
   ├─ /api/minecraft/*   PWA: status, players, import, snapshots, settings, allowlist
   ├─ minecraft drain    chat questions in, companion replies out (aprslog shape)
   ├─ snapshot → BlobStore/BackupShelf, world index → app.mc_* tables
   └─ companion agent    LlmRouter + read-only mc_* tools only
 supervisor ── start/stop/restart/logs of the container (existing routes)
```

**The wrapper is the key component.** BDS has no RCON. Its only control channel is the
console: commands on stdin, output on stdout. Whoever owns that pipe owns graceful stop, hot
backups, and the companion bridge. So the sidecar is a thin Python wrapper that starts BDS as a
child process. It is not a stock image with a bash entrypoint.

`itzg/minecraft-bedrock-server` is the reference for downloading BDS and looking up versions.
M0 decides whether to base `Dockerfile.minecraft` on that image or on a plain Ubuntu base that
reuses its download logic. In both cases the wrapper owns the process.

Wrapper HTTP surface (internal network, bearer token, as for the SDR sidecar):

| Route | Does |
|---|---|
| `GET /status` | Server state, BDS version, world name, online players (parsed from the `Player connected/disconnected` log lines), uptime. |
| `POST /command` | Runs a console command **from a fixed allowlist** (`list`, `say`, `tellraw`, `allowlist …`, `locate …`, `scriptevent jb:…`). There is no free-form console from the PWA in M1. A raw console is an owner-only decision for later. |
| `POST /snapshot` | Runs `save hold` → polls `save query` → streams a tar of the file list, truncating each file to the length `save query` reports → `save resume`. The result is a consistent copy taken while players stay connected. |
| `POST /world/import` | Accepts a streamed `.mcworld` or zip. Only allowed while BDS is stopped. |
| `GET /world/index` | Parses the latest snapshot (never the live DB, which BDS holds locked) and streams JSONL records (M4). |
| `GET /bridge/events`, `POST /bridge/reply` | The companion bridge (M5). |

Graceful stop: the wrapper traps SIGTERM, sends `stop`, and waits for BDS to exit. The compose
service sets `stop_grace_period: 60s` so a supervisor stop or a box update never kills a world
mid-save.

## 3. The waves

### M0 — On-box spike (throwaway, decides the unknowns)

These are run once on the real box with the owner's actual world. No product code merges. The
output is a short findings section added to this doc.

1. **Version match.** Find which game version the owner's world was last saved with, and which
   BDS version opens it. Bedrock clients only join a server on their **exact** version, and
   clients auto-update. So BDS updates are a recurring operational need, not a one-time
   install. M3 builds on this result.
2. **Memory and CPU** with the owner's world loaded and 1–3 players, so the `mem_limit` can be
   set.
3. **Console seams.**
   - Does `save hold/query/resume` behave as documented with this world?
   - Does `execute as <player> at @s run locate structure mansion`, sent through stdin, print
     its result on stdout so the wrapper can capture it?
   - Same question for `locate biome`.
4. **Script bridge without experiments** (the crux of M5):
   - Does a behavior pack using **only stable** `@minecraft/server` APIs load on this world
     without turning on any experimental toggle?
   - Does `scriptevent jb:<id> <json>` on stdin reach `system.afterEvents.scriptEventReceive`?
   - Does `console.log` from the script appear on BDS stdout, and at what length limit?
   - Can a stable **custom slash command** (`/jb:ask <text>`) be registered for all players?
   - What does adding the pack do to achievements on this world? Record the answer for the
     owner.
5. **Parser.** On one snapshot, list chunks, actors (entities), block entities, and the
   per-chunk hardcoded spawn areas, using a Python LevelDB-for-Bedrock reader (`amulet-core`
   with `leveldb-mcpe` bindings, or a minimal reader of our own). Record counts and how long
   the parse takes.

Exit: each item has an answer. Any "no" reshapes the wave it feeds before that wave is
scheduled.

### M1 — The server container and its lifecycle

- `deploy/Dockerfile.minecraft` and `deploy/minecraft/` (the wrapper), a `minecraft` profile,
  and a `minecraft` network shared only with `api`. Hardened like the SDR sidecar, with
  `mem_limit: ${MC_MEM_LIMIT:-3g}`. The volume is `jbrain_minecraft`. Port
  `${MC_BIND:-0.0.0.0}:19132:19132/udp` (plus 19133 for IPv6 if wanted). The EULA is accepted
  by an explicit owner toggle, never by a default.
- **Enabling without a terminal**: a PWA toggle (**Ops → Minecraft → Enable**) queues the
  intent in the settings store. The next **Ops → Update** reads it, as `local-models-sync.sh`
  does, adds `--profile minecraft`, and creates the container. After that, start and stop are
  the existing supervisor routes. Disabling queues the reverse, and the world volume is kept.
- A Minecraft card on `OpsScreen`: state, version, players online, start/stop/restart, logs.
  The same actions go on the debug router.
- First boot with no imported world creates a fresh world, so the server is playable before M2.
- Tests: wrapper unit tests against a fake BDS script (stdin/stdout), the compose logging test,
  ops proxy tests, and a frontend card test.

### M2 — Import the owner's world, and server settings

- **Import**: **Ops → Minecraft → Import world** uploads a `.mcworld` (zip). The backend stores
  it through `BlobStore`, then validates it: it must contain `level.dat`, `levelname.txt` and
  `db/`, its size must be under the limit, and there must be no path traversal. With the
  server stopped, the backend streams it to `/world/import`. The wrapper unpacks it into
  `worlds/<name>` and points `level-name` at it. The previous world is kept, not overwritten,
  so a bad import is undone by switching back.
- How the owner gets the file depends on the platform, and the PWA help text covers each one:
  - **Windows/Android/iOS**: in-game **Edit world → Export world** produces a `.mcworld`.
  - **Realms**: download the world to a device first, then export it.
  - **Xbox/Switch/PlayStation**: there is no export path. The world has to be moved through a
    Realm or a device that can export.
- **Settings**: a small editable subset of `server.properties`, covering server name,
  gamemode, difficulty, allow-cheats, max players, view and tick distance, and online-mode.
  The settings are written by the wrapper and applied on restart.
- **Allowlist**: add or remove gamertags from the PWA, through `allowlist add/remove` on the
  console, so it is live without a restart. The allowlist is **on by default**, because the
  port is published.
- Tests: zip validation (traversal, missing `db/`, oversize), the import state machine, and
  settings round-trip.

### M3 — Snapshots, backups, restore, and BDS updates

- **Snapshot** = the wrapper's `/snapshot` stream. The backend writes it as a dated `.mcworld`
  to the backup shelf through the storage abstraction. Every artifact is therefore something
  the owner can **download and open in their own client**, which is the backup format players
  understand.
- **Scheduled** through the workflow scheduler, not host cron, so the cadence is set in the
  PWA. A suggested default is hourly while players are online and daily otherwise, with
  retention modelled on `backup.sh` (dense for 48 h, then one per day for N days). A snapshot
  also runs automatically before any BDS update, world switch, or restore.
- **Restore**: pick a snapshot, and the server stops, the current world is snapshotted, the
  chosen one is swapped in, and the server starts again. All of this is one PWA action with a
  confirm dialog.
- **BDS updates**: the card shows "server X.Y / latest X.Z" and offers **Update server**, which
  snapshots and then fetches the new BDS into the volume before restarting. Clients
  auto-update, so a lagging server locks every player out. That makes this the most-used
  control after start/stop.
- Decision for the owner: whether Minecraft snapshots also go into the whole-box `jbrain`
  export. If the volume is added to `backup.sh`, then `restore.sh` must change in step.
- Tests: the snapshot protocol against the fake BDS (hold → query → truncate → resume, and
  resume still runs on error), retention, and restore ordering.

### M4 — World index (the companion's long-term memory of the world)

- After each snapshot, the wrapper parses **the snapshot copy** and streams JSONL. The backend
  upserts it into owner-RLS'd tables:
  - `app.mc_snapshots`
  - `app.mc_chunks` (explored chunks per dimension)
  - `app.mc_entities` (type, position, dimension, name tag, snapshot)
  - `app.mc_block_entities` (chests and their contents, signs, spawners, beds)
  - `app.mc_structures` (from the per-chunk hardcoded spawn areas: fortress, witch hut, ocean
    monument, pillager outpost)
  - `app.mc_players` (last position, spawn, dimension)

  Each table gets an RLS isolation test.
- The native LevelDB dependency stays **in the sidecar image**, not in the backend.
- Everything the index answers comes from **explored, saved** chunks and is only as fresh as
  the last snapshot. Answers carry that age ("as of 20 min ago").
- An optional owner-side read tool (`minecraft_world`) lets jerv answer the owner's own
  questions ("which chest has my diamonds?"). It is optional, and it drops out like the SDR
  tools when the feature is off.

### M5 — The companion bridge (in-game ↔ backend)

- A **behavior pack** (`deploy/minecraft/pack/`, TypeScript compiled to the pack's JS) is
  installed into the active world by the wrapper. It uses only stable APIs, as confirmed in M0.
- **Inbound (player asks)**: `/jb:ask where's the nearest pig` (a custom command). If M0 shows
  that chat events are stable, a chat prefix such as `@jb …` is offered as well. The script
  logs one structured line carrying the asker, the text, and the asker's position, dimension
  and facing. The wrapper parses it and queues a `question` event.
- **Live queries (backend asks the world)**: the wrapper sends
  `scriptevent jb:q <id> <json>`. The script runs it against **loaded** chunks (for example
  `dimension.getEntities({type, location, closest: 1})`) and logs the result tagged with
  `<id>`.
- **Outbound (reply)**: `tellraw <asker> {…}` via the console. Replies are private to the asker
  by default.
- The drain loop follows the `aprslog` shape. Everything that comes from players is
  **untrusted input**.

### M6 — The companion agent

- A dedicated persona (the name is the owner's choice) with **only** read-only `mc_*` tools.
  It has no notes, wiki, web, or other domains. It runs on the most restrictive scope there
  is. Players are not principals, and a player's message must never reach the owner's
  knowledge base. This is enforced by the tool set and the session scope, not by the prompt.
- Tools:
  - `mc_nearest_entity(type)`
  - `mc_locate_structure(kind)`
  - `mc_locate_biome(biome)`
  - `mc_find_container(item)`
  - `mc_player_context()`, which returns the asker's position, dimension, last death and spawn
  - `mc_world_info()`, which returns time, weather and day count
- **Answer cascade**: the tools try the freshest source first and always say which one
  answered.
  1. **Live**: the script's query against loaded chunks. This is accurate now, but only near
     players.
  2. **Index**: the last snapshot, covering everything explored. It is stale by the snapshot's
     age.
  3. **Worldgen**: `locate structure|biome` run at the asker's position. It covers places
     nobody has visited yet, such as **the woodland mansion**, because mansions are not
     recorded in saved data until they are generated.

  Results are formatted for chat: distance, compass direction from the asker, coordinates,
  and Nether ÷8 conversion when the dimensions differ.
- **Guardrails**:
  - The bot never runs a mutating command. The wrapper's console allowlist is the
    enforcement, not the prompt.
  - Per-player rate limits.
  - Bounded reply length.
  - It does not reveal another player's location unless the owner turns that on.
- Model: local by default (no per-question cost and no data leaves the box), through the
  adapter. Latency must suit chat, so measure it, with a target of a reply in under 5 s.
- Tests: tool handlers against fixture index rows and a fake bridge, cascade ordering, the
  refusal/scope test (a player asks for notes and gets nothing), and the rate limit. The LLM
  is faked.

### M7 — Nice-to-haves (each is its own small decision)

- "Where did I die?" (from the script's death event, kept per player) and a player
  "remember this spot as *home*" waypoint table.
- A visible **companion NPC** entity, which would need a resource pack that clients download.
- A rendered top-down map in the PWA from the index.
- Multiple worlds (a library, with one active at a time).

## 4. Open decisions for the owner

1. **Who plays, and from where?** LAN only (Bedrock's LAN discovery shows the server in the
   Friends tab) or friends over the internet? The Cloudflare Tunnel carries HTTP only, so
   remote play needs either a router port-forward of UDP 19132 or a UDP relay such as
   Tailscale or playit.gg. Either one is a host or router step (§7).
2. **Which devices?** Phones and Windows can add a custom server address. Consoles cannot
   easily, and need a workaround (LAN discovery on the same network, or a DNS-redirect trick).
3. **The world file**: which platform it is on now, and roughly how big.
4. **Behavior pack consent**: the companion needs a pack on the world. M0 reports what that
   does to achievements on this world before the owner decides.
5. **Companion name and trigger**: `/jb:ask` versus a chat prefix.
6. **Privacy between players**: may the bot say where other players are? The default is no.
7. **Snapshot cadence and retention**, and whether Minecraft snapshots join the whole-box
   export.

## 5. Risks

- **Version churn**: clients auto-update, so a server that lags locks everyone out. This is why
  M3's one-click update exists.
- **Unofficial format**: the LevelDB chunk and actor format changes between game versions. The
  index is best-effort, and an unparseable record is skipped and counted, never fatal.
- **Bridge on stable APIs**: if M0 shows that something the bridge needs is beta-only, the
  fallback is the BDS-only `@minecraft/server-net` HTTP module. That requires experimental
  toggles, which permanently mark the world, so it goes to the owner as a decision and is
  never applied silently.
- **Memory pressure** next to the local models. This is handled by the `mem_limit` and by
  stopping the server when it isn't being played.

## 6. Out of scope

Java Edition (a different server, different protocol, and RCON). Mods, Realms hosting, and
public or unallowlisted servers. Letting the bot build, teleport, or give items.

## 7. Terminal-dependency gaps (non-negotiable #10)

- **Publishing UDP 19132 on the LAN** comes for free with the compose `ports:` entry once the
  profile is created by an in-PWA update. No host step is needed.
- **Internet play** needs a router port-forward. That is outside the box, and it is the
  owner's router. Alternatively, a relay sidecar could be designed in later as its own
  profile.
- **Nothing else** in M1–M6 needs host access. If M0 finds a step that does, it gets designed
  out before the wave is scheduled.
