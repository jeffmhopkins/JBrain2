# Minecraft Bedrock — an on-box world server, its backups, and a companion that knows the world

> **Status:** Proposed · **Last verified:** 2026-10-10 · **Waves:** M0◻️ M1◻️ M2◻️ M3◻️ M4◻️ M5◻️ M6◻️ M7◻️ R1◻️

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
| `GET /worlds` | The world slots on the volume: folder, `levelname.txt`, size, and which slot is active. |
| `POST /worlds/{slot}/import` | Accepts a streamed `.mcworld` or zip into a slot. Refused for the active slot while BDS is running. |
| `POST /worlds/{slot}/activate` | Points `level-name` at the slot. Done while the server is stopped; the backend runs the stop → snapshot → activate → start sequence. |
| `POST /worlds/{slot}/reset` | Empties a slot or regenerates it from a seed. Refused for the active slot while BDS is running. |
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
2. **Memory and CPU** with the owner's world loaded and 1–4 players, so the `mem_limit` can be
   set.
3. **LAN discovery from the Xbox.** Does the server appear under **Friends → LAN Games** on
   the owner's Xbox when the port is published from a bridge network? If not, does it appear
   with `network_mode: host`? This decides M1's networking.
4. **Console seams.**
   - Does `save hold/query/resume` behave as documented with this world?
   - Does `execute as <player> at @s run locate structure mansion`, sent through stdin, print
     its result on stdout so the wrapper can capture it?
   - Same question for `locate biome`.
5. **Script bridge without experiments** (the crux of M5):
   - Does a behavior pack using **only stable** `@minecraft/server` APIs load on this world
     without turning on any experimental toggle?
   - Does `scriptevent jb:<id> <json>` on stdin reach `system.afterEvents.scriptEventReceive`?
   - Does `console.log` from the script appear on BDS stdout, and at what length limit?
   - Is there a **stable chat event**, so `Dave, …` typed in plain chat can be caught? This is
     what decides whether Xbox players can ask from a controller comfortably.
   - Can a stable **custom slash command** be registered for all players? Commands are
     namespaced (`jb:dave`), so check whether players can type plain `/dave`.
   - What does adding the pack do to achievements on this world? Record the answer for the
     owner.
6. **Parser.** On one snapshot, list chunks, actors (entities), block entities, and the
   per-chunk hardcoded spawn areas, using a Python LevelDB-for-Bedrock reader (`amulet-core`
   with `leveldb-mcpe` bindings, or a minimal reader of our own). Record counts and how long
   the parse takes.

Exit: each item has an answer. Any "no" reshapes the wave it feeds before that wave is
scheduled.

### M1 — The server container and its lifecycle

- `deploy/Dockerfile.minecraft` and `deploy/minecraft/` (the wrapper), a `minecraft` profile,
  and a `minecraft` network shared only with `api`. That network is **not** `internal: true`,
  because BDS needs outbound internet for Xbox Live sign-in and its own updates. Hardened like
  the SDR sidecar, with
  `mem_limit: ${MC_MEM_LIMIT:-2g}` (1–4 players on a small world; M0 confirms the figure). The
  volume is `jbrain_minecraft`. Port `${MC_BIND:-0.0.0.0}:19132:19132/udp` (plus 19133 for
  IPv6 if wanted). The EULA is accepted by an explicit owner toggle, never by a default.
- **LAN discovery is a requirement, not a nicety.** An Xbox can't type in a server address, so
  on the home network it joins through **Friends → LAN Games**. That list is filled by a
  broadcast ping on UDP 19132, which the server has to answer. Docker's bridge networking
  often doesn't pass broadcasts through to a published port, so M0 tests this. If the test
  fails, the fallback is `network_mode: host` for this one container. That gives up the
  isolated `minecraft` network, and the backend reaches the wrapper through the host gateway
  with its bearer token. Windows can use either the LAN list or the box's address.
- **Enabling without a terminal**: a PWA toggle (**Ops → Minecraft → Enable**) queues the
  intent in the settings store. The next **Ops → Update** reads it, as `local-models-sync.sh`
  does, adds `--profile minecraft`, and creates the container. After that, start and stop are
  the existing supervisor routes. Disabling queues the reverse, and the world volume is kept.
- A Minecraft card on `OpsScreen`: state, version, players online, start/stop/restart, logs.
  The same actions go on the debug router.
- First boot with no imported world generates a fresh world in slot 1, so the server is
  playable before M2.
- Tests: wrapper unit tests against a fake BDS script (stdin/stdout), the compose logging test,
  ops proxy tests, and a frontend card test.

### M2 — World slots, importing the owner's world, and server settings

- **World slots (owner request, 2026-10-10).** The server holds several worlds, with **one
  loaded at a time**. A slot is a folder under `worlds/` plus a row in `app.mc_world_slots`.
  The row holds a slot id, a display name, its origin (imported, generated with a seed, or
  empty), the seed if known, created and last-played times, and whether Dave is enabled for
  it. The default is **5 slots**, a setting that can be raised. Small worlds make that cheap
  on disk. The table is owner-RLS'd and has an isolation test.
- **Ops → Minecraft → Worlds** lists the slots, marks which one is active, and shows each
  one's size and last backup. Per slot:
  - **Load**: switch the server to this world. If players are online, the server warns them in
    chat ("switching worlds in 30 s"). Then it stops, takes an automatic snapshot of the
    world that was active, activates the new slot, and starts. One confirm dialog.
  - **Import**: upload a `.mcworld` into an empty slot, or over an existing one. Overwriting
    takes a snapshot of that slot first.
  - **New world**: fill an empty slot with a freshly generated world. The owner sets the name,
    an optional seed, the game mode and the difficulty. The world is generated the first time
    the slot is loaded.
  - **Reset** lands in M3, because it depends on M3's snapshots (see M3).
  - **Rename**.
- **Import validation**: the upload is stored through `BlobStore` and then checked. It must
  contain `level.dat`, `levelname.txt` and `db/`, stay under the size limit, and contain no
  path traversal. Then it is streamed to `/worlds/{slot}/import`. A bad import never touches
  another slot.
- The owner's world is on **Windows** and is small (about 3 hours of building). Export it from
  **Play → the world's pencil (Edit) → Export World**, which saves a `.mcworld`, then upload
  that file from the PWA on the same PC. The PWA help text shows these steps. Because the
  world is small, a size limit of a few hundred MB is plenty, and the upload doesn't need to
  be resumable. Going the other way, any backup from M3 opens on Windows by double-clicking it.
- **Settings**: a small editable subset of `server.properties`. Some are **server-wide**:
  server name, max players, view and tick distance, and online-mode. Some are **per slot**:
  gamemode, difficulty, and allow-cheats. The wrapper writes them, and a slot's values are
  applied whenever that slot is loaded.
- **Allowlist**: add or remove gamertags from the PWA, through `allowlist add/remove` on the
  console, so it is live without a restart. The allowlist is **on by default**, because the
  port is published.
- Tests: zip validation (traversal, missing `db/`, oversize), the import and load state
  machines (stop → snapshot → activate → start, and a failed start leaves the previous slot
  recoverable), the slot RLS isolation test, and settings round-trip.

### M3 — Snapshots, backups, restore, slot reset, and BDS updates

- **Snapshot** = the wrapper's `/snapshot` stream. The backend writes it as a dated `.mcworld`
  to the backup shelf through the storage abstraction. Every artifact is therefore something
  the owner can **download and open in their own client**, which is the backup format players
  understand.
- **On demand only (owner decision, 2026-10-10).** Backups happen when the owner presses
  **Back up now**, with an optional label such as "before the castle". There is no schedule.
  The only automatic snapshots are safety nets: one runs before a BDS update, a world import,
  load or reset, and a restore. Those are labelled as automatic.
- Retention is by count: keep the newest 20, with automatic ones expiring first. The owner can
  **pin** a backup to keep it forever and can delete any backup. A small world makes each
  snapshot a few MB, so the count is generous. Scheduled backups through the workflow
  scheduler are deferred to M7. Adding them later is a scheduler entry that calls the same
  route, with no redesign.
- **Backups belong to a slot.** Each snapshot records its slot, the list is filtered by
  slot, and the 20-per-slot retention counts each slot separately.
- **Restore**: pick a snapshot, and its slot is snapshotted and then replaced with the chosen
  one. If that slot is the active one, the server stops and restarts around the swap. A
  snapshot can also be restored **into a different slot**, for example to try out an old
  version without losing the current one. All of this is one PWA action with a confirm
  dialog.
- **Reset a slot (owner request, 2026-10-10).** A snapshot is always taken first, so a reset
  can be undone from the backup list. The owner chooses one of:
  - **Same seed**: regenerate the original terrain fresh, with all building gone. Offered only
    when the slot's seed is known.
  - **New seed**: a brand-new world in the slot.
  - **Empty**: free the slot.

  If the slot is active, the server stops, resets it, and starts again (the empty choice
  isn't offered for the active slot). Because this destroys the world, the confirm dialog
  asks the owner to **type the slot's name**. The slot's world index (M4) is cleared with it,
  so Dave never answers from the old world.
- **BDS updates**: the card shows "server X.Y / latest X.Z" and offers **Update server**, which
  snapshots and then fetches the new BDS into the volume before restarting. Clients
  auto-update, so a lagging server locks every player out. That makes this the most-used
  control after start/stop.
- **Separate from the box backups (owner decision, 2026-10-10).** Minecraft backups do **not**
  go into the whole-box `jbrain` export or into `backup.sh`, and `jbrain_minecraft` is left
  out of both, as the jcode volumes are. They live on their own shelf. Because of that, the
  only copy that leaves the box is one the owner **downloads**, so the card shows when the
  last download happened.
- Tests: the snapshot protocol against the fake BDS (hold → query → truncate → resume, and
  resume still runs on error), count retention with pins, and restore ordering.

### R1 — Remote play for the brothers: a public address, nothing to install

**Owner decision (2026-10-10): the brothers install nothing.** They type an address and a port
into **Servers → Add Server** in Minecraft on Windows. That rules out the Cloudflare tunnel.
The tunnel carries UDP only to WARP clients, and public UDP (Spectrum) is an Enterprise
feature. So the box itself has to be reachable from the internet on UDP. The `minecraft`
container must already reach the internet **outbound** anyway: BDS checks each player's Xbox
Live sign-in (`online-mode`) and downloads its own updates. So its network can't be
`internal: true`.

Inbound reachability is set up by a **reachability helper** in the wrapper. It is driven from
**Ops → Minecraft → Internet play**, and it picks the first path that works:

1. **Direct (UPnP / NAT-PMP).** The helper asks the home router to forward UDP 19132 to the
   box. That is the same mechanism consoles and the Xbox app use, so there is no router
   admin page and no terminal. The helper renews the mapping while the server runs and
   removes it on stop or when internet play is switched off. It first checks for **CGNAT**:
   if the router's WAN address is private or in `100.64/10`, or differs from the public
   address an outside echo reports, then a forward can't work and it goes straight to the
   relay. UPnP discovery is multicast on the LAN, so it needs the same host-network reach as
   LAN discovery (M0 item 3).
   - **Address**: the box keeps a **DNS-only** (grey-cloud) record such as `mc.<domain>`
     pointed at the home IP, through the Cloudflare API, with a scoped token entered in the
     PWA. The record is updated whenever the IP changes. The brothers type
     `mc.<domain>`, port `19132`.
   - If UPnP is off on the router, the card says so. The owner can either turn on UPnP in the
     router's own app or add one manual forward rule. Both are router steps, not box steps.
     Or the owner can choose the relay.
2. **Relay (playit.gg-style), for CGNAT or no-UPnP.** An agent container in the `minecraft`
   profile dials **out** to the relay service, which hands back a public address and port for
   Bedrock UDP. That works behind CGNAT, the same way the tunnel does for the web. The agent
   is claimed once through a link that the **PWA shows**. The Ops card then shows the
   brothers' address and port. The cost is a third party and a hop. The free tier assigns
   the address and port; a small paid tier pins them.
3. **Later, only if both of those disappoint**: a small VPS the owner rents as a self-owned
   relay (WireGuard plus a UDP forward). It is not built now. It would need its setup
   designed so it isn't shell-only.

**Verifying from outside.** The box can't test its own public address from inside (hairpin
NAT lies). So the card's **Test** button asks a public Bedrock status service to ping the
address from the internet. That gives a real outside answer, and the card shows the result
(reachable, the server's MOTD and version, and how long the ping took). No brother has to be
online to check it.

**Exposure rules, now that the port is public:**

- The **allowlist** (M2) is required. Internet play won't switch on with it off.
- `online-mode` stays true, so every player is a signed-in Xbox account.
- Only UDP 19132 is exposed. The wrapper's HTTP control port stays on the internal network.
- Turning internet play **off** removes the UPnP mapping or stops the relay agent.
- The card always shows whether the server is publicly reachable right now.

A remote **Xbox** player would still be stuck, because an Xbox can't type an address. That
case would need an MCXboxBroadcast-style broadcaster. It is recorded here and not built.

Exit: **Test** reports the server reachable from outside, and one brother joins from his own
house by typing the address.

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

  Every row in these tables is keyed by **slot**. Dave answers only about the active slot.

  Each table gets an RLS isolation test.
- The native LevelDB dependency stays **in the sidecar image**, not in the backend.
- Everything the index answers comes from **explored, saved** chunks and is only as fresh as
  the last snapshot. Answers carry that age ("as of 20 min ago").
- An optional owner-side read tool (`minecraft_world`) lets jerv answer the owner's own
  questions ("which chest has my diamonds?"). It is optional, and it drops out like the SDR
  tools when the feature is off.

### M5 — The companion bridge (in-game ↔ backend)

- A **behavior pack** (`deploy/minecraft/pack/`, TypeScript compiled to the pack's JS) is
  installed by the wrapper into each slot that has Dave enabled, at the moment that slot is
  loaded. That covers new, imported, and reset worlds without any manual step. It uses only
  stable APIs, as confirmed in M0.
- **Inbound (player asks)**: a chat message addressed to the companion by name, such as
  `Dave, where's the nearest pig?` or `@dave …`. This matters for the Xbox players, who would
  struggle to type a slash command on a controller, so it depends on M0 finding a stable chat
  event. `/dave <question>`, a custom command (namespaced as `jb:dave`), is the fallback and is always registered. The
  name is a **setting**, not a constant: the script reads it from a value the wrapper pushes
  through `scriptevent`, so renaming needs no pack rebuild. The script
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

- A dedicated persona named **Dave** (owner decision, 2026-10-10). The name is changeable in
  Ops → Minecraft, and one setting feeds the persona prompt, the chat trigger, and the reply
  prefix. Dave has **only** read-only `mc_*` tools.
  It has no notes, wiki, web, or other domains. It runs on the most restrictive scope there
  is. Players are not principals, and a player's message must never reach the owner's
  knowledge base. This is enforced by the tool set and the session scope, not by the prompt.
- Tools:
  - `mc_nearest_entity(type)`
  - `mc_locate_structure(kind)`
  - `mc_locate_biome(biome)`
  - `mc_find_container(item)`
  - `mc_player_context()`, which returns the asker's position, dimension, last death and spawn
  - `mc_where_is(player)`, which returns another player's position, live if they're online and
    otherwise their last saved position
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
  - Other players' locations are **shared** ("they're all friends", owner decision,
    2026-10-10). A setting can turn sharing off later.
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
- Scheduled backups: a workflow-scheduler entry that calls M3's on-demand route.

## 4. Owner decisions

**Decided (2026-10-10):**

- **Players**: 1–4 people, mostly on the home network. Brothers may join over the internet,
  which is wave R1.
- **Remote play**: the brothers **install nothing**. The server gets a public address
  through a UPnP forward plus a DNS-only Cloudflare record, or through a UDP relay when the
  connection is behind CGNAT (R1). The Cloudflare tunnel can't do this, because it carries UDP
  only to people running Cloudflare's WARP app.
- **Devices**: Windows and Xbox on the home network. Remote players (the brothers) are on
  Windows only, so they join by address and no Xbox broadcaster is needed. The home Xbox means
  LAN discovery has to work (M0 and M1).
- **Add-on**: yes. The companion is a behavior pack, which is an add-on. A behavior pack with
  no resource pack shouldn't trigger the "download add-ons" prompt for players, and M0
  confirms that. Experimental toggles are still never turned on without asking.
- **Companion**: named **Dave** for now, and changeable later (M5, M6).
- **Player locations**: Dave may say where other players are.
- **Box backups**: Minecraft backups stay separate from the whole-box export (M3).
- **World slots**: several worlds live on the server, one loaded at a time. Each slot can be
  loaded, imported, created fresh, renamed, and **reset** (M2, M3).
- **World**: a small world on Windows, about 3 hours of building. It is imported by exporting
  a `.mcworld` (M2).
- **Backups**: on demand for now, plus automatic safety snapshots before risky actions (M3).
  Scheduled backups wait for M7.

**Still open:**

1. **Achievements**: M0 reports what the pack does to achievements on this world, for the
   record. The owner has already accepted the add-on.

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
- **Internet play (R1)** is switched on from the PWA. The UPnP mapping, the DNS record, the
  relay claim link and the outside test are all on the Ops card. The only possible non-box
  step is turning on UPnP or adding a manual forward in the router's own app, if the router
  has UPnP off. No step touches a box terminal.
- **Nothing else** in M1–M6 needs host access. If M0 finds a step that does, it gets designed
  out before the wave is scheduled.
