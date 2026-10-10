# Minecraft Bedrock — an on-box world server, its backups, and a companion that knows the world

> **Status:** In progress · **Last verified:** 2026-10-10 · **Waves:** M0◻️ M1✅ M2◻️ M3◻️ M4◻️ M5◻️ M6◻️ M7◻️ M8◻️ M9◻️ R1◻️ P1◻️ P2◻️ P3◻️

The owner wants a Minecraft **Bedrock** dedicated server on the box. They need to start and
stop it, back up its world, and **import an existing world** they already play. On top of
that, they want a **chat companion** that players talk to from inside the game and that
answers from the world's real data: "where's the nearest pig?", "where's the nearest woodland
mansion?", "where did I die?".

This doc records the plan in phases. **M0a (the debug-driven rig) is built**; everything
else is not. Promoted out of `../proposed/` on 2026-10-10, when the owner asked for M0a
with the server **on by default**. The phases are ordered so that each one
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
                                        ▲  HTTP + bearer, via host.docker.internal (host network)
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

### M0 — Spike rig, driven through the debug API (a fresh world first)

**Owner decisions (2026-10-10):**

- **Home network only.** Nothing in M0 touches the router or the internet path. Router
  reachability is the first step of R1.
- **Test on a new, empty world first.** The owner's world is imported only after the rig
  proves out.
- **The assistant drives the setup and debugging with a debug token** that the owner hands
  over. So M0 is **not** a throwaway shell spike. It merges a minimal rig, and every probe
  below is run through `/api/debug/minecraft/*` (§3a). The owner's only jobs are handing over
  the token and joining from the Windows PC and the Xbox when a probe needs a real client.

**M0a — built (2026-10-10).** What merged:

- `deploy/minecraft/` — a stdlib-only wrapper:
  - `install.py` downloads BDS into the volume from Mojang's download index, following
    `MC_BDS_VERSION` (default `latest`). An update never overwrites `server.properties`,
    the allowlist, the permissions file, or `worlds/`.
  - `bds.py` owns the console. It collects each command's reply, tracks players from the
    log, refuses `stop` and `save …` (each has its own route), takes hot `.mcworld`
    snapshots through `save hold`/`query`/`resume`, and stops gracefully.
  - `server.py` serves the HTTP control surface (bearer-guarded): `/status`, `/logs`,
    `/command`, `/snapshot`, `/snapshots`, and `/properties` (owner overrides layered on the
    compose defaults, applied at the next start, with the ports pinned).
- `deploy/Dockerfile.minecraft`, and the `minecraft` compose service: **in the stock stack,
  with no profile**, at the owner's request. It has a 2 GB `mem_limit`, a 60 s stop grace,
  and the `minecraft` volume, which is kept out of the box backups. M0a shipped it on its
  own bridge network with UDP published; the M0b finding below moved it to the **host
  network**, with a bearer on the control port.
- **Defaults chosen for the LAN-only test**:
  - `allow-list=false`. Nothing is forwarded from the router, and an empty allowlist would
    lock the family out.
  - `content-log-console-output-enabled=true`, so script output reaches the console for
    M5.
- `/api/debug/minecraft/*` (§3a) and the `debug-connect.sh minecraft` verb.

**Pre-merge check (2026-10-10)**: the image builds, and the wrapper installed and launched
Mojang's real 1.26.52.3 binary, which created the seeded world and printed the version
line the wrapper parses. The binding step failed only because that sandbox kernel has no
IPv6 at all, and RakNet aborts when it cannot open its IPv6 socket. Docker containers on
the box have the IPv6 address family even without IPv6 routing, but **M0b's first status
read confirms the server binds**. If it doesn't, the fix is a compose sysctl, not a code
change.

**M0b findings so far (2026-10-10), on the box with the debug token:**

- **It runs.** `ffb9713` deployed, and the container came up healthy. BDS 1.26.52.3
  downloaded itself, generated the world, and bound IPv4 and IPv6, so the sandbox's IPv6
  failure did not recur. The console (`list`, `time query daytime`) and a hot
  `.mcworld` snapshot both worked against the real server.
- **RakNet is dead in 1.26.** Started on `transport=raknet`, the server ran and then logged
  *"NetherNet is the only supported transport type. Players will not be able to connect to
  your game without NetherNet."* Switched to `nethernet` through the properties override,
  it started cleanly. Mojang's bundled how-to says NetherNet:
  - signals over **TCP** on `server-port` (an HTTP handshake);
  - negotiates gameplay **UDP** ports per client, from the ephemeral range unless
    `server-udp-ports` pins them;
  - advertises the server's **own** addresses to the client.

  On a bridge network that address is the container's 172.x and LAN broadcasts never
  arrive, so **the follow-up PR moves the container to `network_mode: host`**. That was the
  plan's named fallback (M0 item 3). It also:
  - makes `nethernet` the default;
  - puts a bearer token (`MINECRAFT_TOKEN`, minted by `update-inner.sh` and `install.sh`)
    on the control port, since that port is now on the LAN;
  - has the api reach the control port through `host.docker.internal:19180`.
- **Implication for R1 (internet play)**: forwarding UDP 19132 alone won't do. NetherNet
  needs TCP 19132 for signaling plus a UDP range pinned with `server-udp-ports`, and its
  advertised mapping (`[public-ip:]external:internal`) has to name the public address.
  R1's step 0 accounts for this.
- **`locate` works from the console, with no player online (item 4 ✅).** `execute
  positioned X Y Z run locate structure <id>` prints its answer on stdout, which the
  wrapper captures. From 0,0 on the fresh world:
  - `village` at 200, 152 (251 blocks);
  - `mansion` at 8120, 8104 (11,472 blocks; `y?` because the structure isn't generated yet,
    which is exactly the worldgen fallback the cascade needs);
  - `locate biome minecraft:cherry_grove` at -1632, 86, -96 (1,634 blocks).

  **Biome ids need the `minecraft:` namespace** (a bare `cherry_grove` is a syntax error),
  and structure ids don't. Dave's tools pass the full id.
- **Memory**: about 280 MB RSS idle with no players. It is re-measured under play.
- **The script bridge (item 5) can't be probed through the console.** Installing a test
  behavior pack means writing into the world folder, and the debug surface deliberately
  has no file write. So the wrapper gained `POST /probe-pack`, which installs the
  **bundled** probe pack (`deploy/minecraft/probe-pack/`, nothing uploaded) into the active
  world and restarts. The pack logs `[jbrain-probe]` lines for: loaded, chat event
  available, the `jb:dave` custom command registered, `scriptevent` echoes, and whether a
  dead player's inventory is readable (the keep-inventory question).
- **Also shipped for M1's backend**: join/leave events with a per-boot id (`GET /events`),
  including synthetic leaves when the server stops; `GET /version` (cached Mojang lookup);
  and `POST /update` (snapshot first, a failed snapshot installs nothing, then install
  and restart). All are on the debug router too.
- **The script bridge works (item 5 ✅, `6384ddb`, 2026-10-10).** With the probe pack
  installed through `/probe-pack`:
  - The pack loaded with **no experiments**. BDS logged `Pack Stack - [00] JBrain probe`
    on stable `@minecraft/server` 2.0.0.
  - `console.log` reaches the server console as `[Scripting] [jbrain-probe] …`, which is
    the return channel.
  - `scriptevent jb:q {"id":1,"ask":"nearest pig"}` sent on stdin was echoed by the script
    **in about 1 ms with the JSON intact**, which is the inbound channel.
  - The **`jb:dave` custom command registered** for all players.
  - **`world.afterEvents.chatSend` is NOT available on stable**, so plain-chat "Dave, …"
    can't be caught without the Beta APIs experiment. **`/jb:dave <question>` is the way
    to ask** (a decision for M5; everyone types, so it costs little).
  - Still needing a player: running `/jb:dave` from a client, and whether a dead player's
    inventory is readable at `entityDie` (the keep-inventory question).
- **Still to run**: Windows and Xbox joining (including whether the Xbox sees the server in
  LAN Games), `/jb:dave` from a client, the inventory-at-death read, memory under play, the
  parser, map and biome inputs, and the vanilla-client checks.

**First boot** generates the world from `MC_LEVEL_SEED` if one is set, otherwise from a
random seed that `/properties` and `level.dat` record. The known-seed checks can set
`level-seed` before the world is regenerated.

**M0b — the probes**, run on the real box over the debug API. The findings are written up as a
short section added to this doc.

1. **Version match.** Which BDS version runs, and does the owner's Windows client (always
   auto-updated) join it? Bedrock clients only join a server on their **exact** version, so BDS
   updates are a recurring need (M3). Afterwards, check the version the owner's own world was
   last saved with.
2. **Memory and CPU** with 1–4 players on the fresh world, from `GET /debug/host`, so the
   `mem_limit` can be set.
3. **LAN discovery from the Xbox.** Does the server appear under **Friends → LAN Games** on
   the owner's Xbox when the port is published from a bridge network? If not, does it appear
   with `network_mode: host`? This decides M1's networking. Switching to host networking is
   a compose change, so it ships as a PR plus `/debug/update`, with no host step.
4. **Console seams.**
   - Does `save hold/query/resume` behave as documented?
   - Does `execute as <player> at @s run locate structure mansion`, sent through stdin, print
     its result on stdout so the wrapper can capture it?
   - Same question for `locate biome`.
5. **Script bridge without experiments** (the crux of M5):
   - Does a behavior pack using **only stable** `@minecraft/server` APIs load on this world
     without turning on any experimental toggle?
   - Does `scriptevent jb:<id> <json>` on stdin reach `system.afterEvents.scriptEventReceive`?
   - Does `console.log` from the script appear on BDS stdout, and at what length limit?
   - Is there a **stable chat event**, so `Dave, …` typed in plain chat can be caught? This is
     a nicety, not a blocker. The family's Xboxes have keyboards, so `/dave …` is just as
     easy to type.
   - Can a stable **custom slash command** be registered for all players? Commands are
     namespaced (`jb:dave`), so check whether players can type plain `/dave`.
   - What does adding the pack do to achievements? Check on the fresh world, then record the
     answer for the owner's world before Dave is enabled on it.
6. **Parser.** On one snapshot of the fresh world, taken after a short play session, list
   chunks, actors (entities), block entities, and the per-chunk hardcoded spawn areas, using a Python LevelDB-for-Bedrock reader (`amulet-core`
   with `leveldb-mcpe` bindings, or a minimal reader of our own). Record counts and how long
   the parse takes.

7. **Map and biome inputs** (for M8):
   - Where does a block→colour table come from? Does BDS ship a vanilla pack with usable
     textures or map colours, or do we keep our own table?
   - Do `cubiomes` biome answers (Java 1.18+ generation) match `locate biome` on the
     known-seed world at ~10 sample points?

8. **Vanilla-client checks**: the §3c list, on Windows and on the Xbox.

Exit: each item has an answer. Any "no" reshapes the wave it feeds before that wave is
scheduled.

### M1 — The Minecraft Ops card: lifecycle, server updates, players

**Built in M0a**, kept here for the record:

- `deploy/Dockerfile.minecraft` and `deploy/minecraft/` (the wrapper), on the **host
  network** (M0b: NetherNet needs the box's real address and LAN broadcast), with
  outbound internet for Xbox Live sign-in and its own updates. The control port is guarded
  by `MINECRAFT_TOKEN`. `mem_limit: ${MC_MEM_LIMIT:-2g}` (1–4 players on a small world).
  The volume is `jbrain_minecraft`. BDS has no EULA file; running it is acceptance of Mojang's EULA and
  privacy policy. The owner chose on-by-default knowing that (2026-10-10), and the
  Dockerfile header says so.
- **LAN discovery is a requirement, not a nicety.** An Xbox can't type in a server address, so
  on the home network it joins through **Friends → LAN Games**. That list is filled by a
  broadcast the server has to answer. This is why the container is on the host network
  (M0b). Windows can use either the LAN list or the box's address.
- **On by default (owner decision, 2026-10-10, built in M0a)**: the service has no profile,
  so the first **Ops → Update** after merge creates and starts it. Start and stop are the
  existing supervisor routes, which Ops already shows for every container.
**M1 is built (2026-10-10).** The GUI gate chose **variant B**, with C's player table
(binding mock `../mocks/minecraft-ops/b-dedicated-screen.html`; three variants, an
independent rendered review, and a revision before the owner chose). What shipped:

- **The Minecraft screen.** A card-launcher tile under SYSTEM, plus an Ops shortcut row
  that doubles as the glance: who's on, or "update available · X — newer clients can't
  join".
- **The owner API** (`/api/minecraft`): status, version with release notes,
  start/stop/restart, update, settings, players, retry-install.
- **Start/Stop/Restart act on the game server inside the container**, so status, server
  facts and updates keep working while it is stopped, and a stop is remembered across
  reboots.
- **Update Minecraft**: backup first, then install, restart, and a real **rollback**.
  If the new version doesn't log "Server started." within 2 minutes, the old version is
  reinstalled and the world restored from the backup. A stopped server stays stopped.
- **Auto-update**: a setting, off by default. With it on and Mojang having something
  newer, the screen's Start and Restart run the same backed-up update, as their confirm
  says, and a container boot backs the world up cold before installing. With it off, a
  start keeps the installed version. Automatic backups are pruned to the newest 10; the
  owner's own snapshots are never pruned.
- **One lifecycle at a time.** First-boot install, Start/Stop/Restart, an update and the
  probe pack share one lock. A Start that arrives mid-install is recorded and acted on
  by the install, and a stopped server's status stays readable throughout.
- **Every install is verified.** A download that fails reads as `failed` ("still on
  X"), never as `done`. If the old version can't be reinstalled during a rollback, the
  server is left **stopped** (`run` off) rather than crash-looping the broken one.
- **Release notes** matched to Mojang's changelog article. The first 4 bullets are quoted
  verbatim, under the article's title.
- **Play history** in `app.mc_player_sessions` (owner-only RLS), drained from the
  sidecar's join/leave events. Replay-safe, closed at the last heartbeat on a crash, and
  keyed by xuid.
- **Warnings on every path that stops the server.** The service-list Stop/Restart for
  `minecraft` and Ops' "Restart all" warn when players are online.

**What M1 adds: a Minecraft card in Ops** (owner requests, 2026-10-10; the spec as
designed). Everything on it is
also reachable over the debug router (§3a).

- **Lifecycle**: the state (`installing` / `running` / `stopped` / `install_failed` with its
  error), uptime, and **Start / Stop / Restart**. Restart is stop-then-start, so the 60 s
  save grace is honoured.
- **Server updates**:
  - **The version check.** The wrapper reads Mojang's download index (`install.py`
    `latest_url`) on demand and on a slow timer, and the card shows *running X / latest Y*.
  - **What changed.** Mojang posts each release's changelog in the "Release Changelogs"
    section of feedback.minecraft.net, which has a public listing API. Titles use the
    marketing number: BDS `1.26.52.3` is "Minecraft Bedrock Edition **26.52** … Changelog".
    So the box can find the article for the new version and show:
    - a **short "what changed"**: the first few bullet points of that article, taken
      **verbatim**, with no model summarising them;
    - a **Release notes** link to the full article.

    If the article isn't posted yet, the card says "release notes not published yet" and
    links to Mojang's general update page (`aka.ms/MinecraftUpdate`). The update still
    works without it.
  - **Update server**: one button. It takes a snapshot (the M0a hot snapshot, labelled
    `pre-update-<old version>`), installs the new BDS into the volume (the wrapper's
    preserve-on-update rules apply), and restarts.

    BDS follows `latest` on every container start today. Once this button exists, that
    automatic follow becomes a setting (*auto-update on restart*: on/off), so the owner
    can pin a version and update only when they choose.
- **Players**:
  - **Online now**: each player with **how long this session has lasted**. The wrapper
    records each join time from the `Player connected` log line.
  - **Time on the server**: total play time, session count, first seen and last seen, per
    player. The api drains join and leave events from the wrapper into
    `app.mc_player_sessions`, keyed by **xuid** so a gamertag change doesn't split a
    player. The table is owner-RLS'd and has an isolation test. If the server or box goes
    down mid-session, no leave line is ever logged, so on startup any open session is
    closed at the last time the server was seen running. Totals never count downtime.
  - **Lifetime stats** (deaths and causes, mobs killed, blocks mined and placed, distance
    travelled) appear here **once the M5 add-on exists**. Bedrock keeps no player
    statistics and its console exposes none, so they can only be counted by the
    behavior-pack script from in-game events. They feed the same per-player rows, and
    Dave's `playtime / leaderboard` tool (§3b).
- First boot with no imported world generates a fresh world in slot 1, so the server is
  playable before M2.
- Tests: the version check against a fixture download index and a fixture changelog listing
  (version → article match, verbatim bullets, the fallback when no article exists); the
  update sequence (snapshot before install, restart after, nothing installed if the
  snapshot fails); session accounting (join/leave, an unclosed session closed at the last
  seen time, xuid as the key); the `mc_player_sessions` RLS isolation test; the ops proxy;
  and the card's frontend tests. GUI gate (`../reference/DESIGN.md`): the card gets a mock
  before it is built.

### M2 — World slots, importing the owner's world, and server settings

**Owner additions (2026-10-10).**

**Every world option is switchable, per world.** That covers difficulty, game mode and
cheats, plus **all of the server's game rules**: 38 on 1.26.52.3, read from `gamerule`,
such as `doFireTick` (fire spread), `keepInventory`, `mobGriefing`, `doDayLightCycle`,
`pvp`, `showCoordinates`, `spawnRadius` and `playersSleepingPercentage`.
- On the loaded world a change applies **live** through the console.
- On any other world it's saved and applied **when that world loads**.
- Every world's saved rules are re-applied on each load, so the world stays as set.
- The rule list and its types come from the server, so a rule added in a later Bedrock
  version shows up with no code change.

**Safety and honesty, from the mock review (2026-10-10).**
- **Chat warning before a stop:** an owner action that stops the loaded world with
  players online (Load, import/reset/restore over it, Stop, Restart, Update) first says
  so in chat and waits 10 seconds.
- **Visible jobs:** the running job and its phase are in the status, so every device
  sees them.
- **Rules re-apply on every start**, not just on a Load.
- **Worlds describe themselves:** an imported or restored world takes its seed, game
  mode, difficulty, cheats and rules from its own `level.dat` (a small little-endian NBT
  read), never from the world it replaced. The first-boot world learns its seed the same
  way, so "reset with the same seed" works for it.

**New-world seed: random or entered.** Random is a box-chosen seed, shown and
re-rollable before creating, so "reset with the same seed" always works. An entered seed
is any text up to 64 characters, as in Bedrock's own seed box.

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
- **BDS updates** moved to M1 (owner request, 2026-10-10), since a lagging server locks out
  every auto-updated client. M3 only adds the pre-update snapshot to the backup list and
  retention.
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

**Step 0 — router reachability probe, done first, when R1 is picked up (moved out of M0
by owner decision, 2026-10-10).** Test the router before anything more drastic.

- **CGNAT check, which the owner can do from a phone with no box involved**: compare the
  WAN/Internet IP shown in the router's app with what a "what is my IP" site reports. If they
  match, a port-forward can work. If the router's WAN IP is in `100.64–100.127.x.x`, `10.x`,
  `172.16–31.x` or `192.168.x`, the connection is behind CGNAT, and only the relay can work.
- Note whether the router's app offers **UPnP** and **port forwarding**.
- On the box: a throwaway UPnP probe maps UDP 19132, and the outside status-service ping
  confirms it from the internet.
- **NetherNet changes what gets forwarded** (M0b): TCP 19132 for signaling plus a pinned
  UDP range (`server-udp-ports`, for example 19140-19149), advertised with the public
  address (`server-udp-ports=<public-ip>:19140-19149:19140-19149`). The UPnP helper maps
  all of them, and the DNS updater keeps that advertised IP current.
- **Gate**: if this passes, build path 1 only. The relay (path 2) is not built unless a
  probe fails.

**Cloudflare-native routes were checked (2026-10-10), and none of them fits.**

| Route | Why it doesn't fit |
|---|---|
| Tunnel public hostname | Carries HTTP/HTTPS only. TCP needs `cloudflared` on the client, and UDP isn't offered. |
| Tunnel + WARP private routing | Carries UDP, but every player has to run the WARP app. The owner rejected that. |
| Spectrum | Not on the Free plan. Pro and Business get one "Minecraft" app, which is the Java/TCP protocol. Generic UDP, which Bedrock needs, is an **Enterprise paid add-on**. Spectrum also proxies *to* the origin's public IP, so the box would still need an inbound path. It doesn't remove the forward or the relay. |
| Proxied (orange-cloud) DNS | Only HTTP ports are proxied. |
| SRV record (to hide the port) | Bedrock ignores SRV records; only Java Edition reads them. |

So Cloudflare's role is **the name**: `mc.hopkinsbrain.com`. It is either a DNS-only record
pointing at the home IP (path 1), or a CNAME to the relay's hostname (path 2). Either way the
brothers type `mc.hopkinsbrain.com` and never see an IP. With the relay, they also type the
port the relay assigns.

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
   - **Address**: the box keeps a **DNS-only** (grey-cloud) record such as `mc.hopkinsbrain.com`
     pointed at the home IP, through the Cloudflare API, with a scoped token entered in the
     PWA. The record is updated whenever the IP changes. The brothers type
     `mc.hopkinsbrain.com`, port `19132`.
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
- **Inbound (player asks)**: `/jb:dave <question>` (M0b: there is no stable chat event, so
  plain-chat `Dave, …` would need the Beta APIs experiment, which is refused). Every player
  types, the family's Xboxes included, since they have keyboards (owner, 2026-10-10). The
  companion's name is a **setting**. Custom commands are registered once, at server
  startup, so the wrapper writes the name into the pack's config before starting BDS.
  Renaming is a restart, not a pack rebuild. The script logs one structured line carrying the asker, the text, and the asker's position, dimension
  and facing. The wrapper parses it and queues a `question` event.
- **Live queries (backend asks the world)**: the wrapper sends
  `scriptevent jb:q <id> <json>`. The script runs it against **loaded** chunks (for example
  `dimension.getEntities({type, location, closest: 1})`) and logs the result tagged with
  `<id>`.
- **Outbound (reply)**: `tellraw <asker> {…}` via the console. Replies are private to the asker
  by default.
- **Typing is the primary interface**, because the family's Xboxes have keyboards (owner,
  2026-10-10). A button menu for the common questions is a later nicety (M7), not part of M5.
- The drain loop follows the `aprslog` shape. Everything that comes from players is
  **untrusted input**.

### M6 — The companion agent

- A dedicated persona named **Dave** (owner decision, 2026-10-10). The name is changeable in
  Ops → Minecraft, and one setting feeds the persona prompt, the chat trigger, and the reply
  prefix. Dave has **only** read-only `mc_*` tools.
  It has no notes, wiki, web, or other domains. It runs on the most restrictive scope there
  is. Players are not principals, and a player's message must never reach the owner's
  knowledge base. This is enforced by the tool set and the session scope, not by the prompt.
- **Tools**: the ★ core set from the catalog in §3b. Later tools are added one at a time, each
  as its own small PR, because each one is just a handler over the same index, bridge and
  console.
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

- The non-★ tools in §3b, one per PR, in whatever order players actually ask for them.
- **A Dave quick menu**: `/dave` with no text opens a server form (`@minecraft/server-ui`
  `ActionFormData`). The form has buttons for *Where am I*, *Nearest…*, *Guide me home*,
  *My last death*, *Map of here* and *Save this spot*. It is vanilla-safe (§3c), and it is
  optional since everyone has a keyboard.
- A visible **companion NPC**. A *custom* entity needs a resource pack. The vanilla client
  downloads that automatically, but it's heavier and breaks the "behavior pack only" rule in
  §3c, so it's the owner's call. The alternative is a **vanilla mob** (an allay or villager
  named "Dave", made invulnerable by the pack), which needs no client assets. Spawning it
  changes the world, so it is an owner action, never a player's.
- Scheduled backups: a workflow-scheduler entry that calls M3's on-demand route.

### M8 — Maps and biomes (after M4; the map tools in §3b)

**Owner request (2026-10-10): the assistant should generate 2D maps of areas and know about
biomes.**

- **Renderer**: Python (numpy + Pillow) in the sidecar, next to the parser. It works from the
  snapshot, never the live world. Per chunk, it takes the **top block** of each column from
  the heightmap and the subchunk palettes. That block is coloured by a block→colour table
  shaped like the in-game map palette, and shaded by height so hills read. M0's parser probe
  checks where the table comes from: BDS's bundled vanilla pack, or a hand-kept table for the
  ~300 common blocks with an "unknown" colour for the rest. A rendered chunk is **cached per
  snapshot** and reused while that chunk is unchanged, so most of a small world re-renders in
  seconds.
- **Layers**:
  - `terrain` (top blocks)
  - `biome`, from the per-subchunk biome palettes (`Data3D`), using the conventional
    biome-map colours with a legend
  - `height` (contours)
  - `slice@Y`, a cave or ore layer, which is gated by fair play (§3b)
  - `explored`, the fog of war: what has been generated at all
  - `changes`, the difference between two snapshots, highlighting blocks changed since then
- **Overlays**: players and their last positions, waypoints, deaths, beds and spawn, indexed
  structures, `locate` results, chosen entity types ("all the pigs"), and a grid with
  coordinates.
- **Biomes beyond the explored edge**: since 1.18, Bedrock and Java share biome and terrain
  generation for a given seed (structures differ). So a seed biome map of **unexplored**
  land can come from `cubiomes` (C, Java 1.18+ biome generation). M0's known-seed world
  checks that claim: compare `locate biome` answers with `cubiomes` output for the same
  seed. If they disagree, that layer is dropped, and unexplored biomes come only from
  `locate biome`, one point at a time.
- **Where maps appear**:
  - The owner sees them in a **Maps** screen in the PWA, with pan and zoom over tiles,
    layer toggles, and pins for slot and snapshot.
  - Owner-side jerv gets a `minecraft_map` tool that returns the image into chat.
  - The assistant gets them through `POST /debug/minecraft/probe {name: "map"}`.
- **Players can't be shown an image in Bedrock chat.** So Dave answers "map of here" with a
  **short-lived share link** (for example 24 h) to that one PNG, served over HTTPS through the
  existing tunnel. HTTP works there, unlike the game's UDP. The link carries an anonymous
  scoped token, the same substrate as the intake/student links. It is rate-limited, shows the
  active slot only, and exposes nothing else. **Bedrock chat links aren't clickable**, so the
  link is a short code that's easy to type, such as `hopkinsbrain.com/m/7KQ2`. Players open
  it in a browser on their PC or phone, and Dave always gives the directions in text as well.
- Storage: rendered PNGs and tiles go through `BlobStore`. They are derived, so they are
  pruned with their snapshot.
- Tests: render a fixture snapshot to golden PNG hashes per layer, check the overlay
  projection (coordinates → pixels, Nether scale), check that share-link scope and expiry
  are refused outside the link, and check that `slice@Y` is refused when fair play is on.

### M9 — The tricorder: a held item that points where Dave said (owner, 2026-10-10)

**The idea.** Dave gives a player a target ("the nearest woodland mansion", "your base",
"where you died"). While that player holds the **tricorder**, their HUD shows which way
to turn and how far it is. Nobody has to read or type coordinates. It works like a
compass that Dave sets.

**What the player sees.**
- Holding the tricorder: actionbar text refreshed about 4 times a second, e.g.
  `↖ Woodland mansion · 412 blocks`.
- The arrow is relative to where the player is **facing**: eight arrows, from the bearing
  to the target minus the player's view direction. Turning re-points it.
- Within about 8 blocks: `You're here — Woodland mansion`, plus one vanilla chime.
- **Target in another dimension:** `Woodland mansion is in the Overworld — take a portal`.
  The Nether pointer can aim at the portal Dave knows from the M4 index.
- **No target yet:** `Ask Dave where to go: /jb:dave …`.
- Putting the item away clears the actionbar. Nothing shows for anyone else.

**How it's built** (on M5's bridge, using M6's `guide_me` tool):
- Dave's `guide_me(player, target)` sends `scriptevent jb:target <xuid> <json>` with
  the label, dimension and x/y/z. The pack stores it as a **dynamic property on the
  player**, so it survives logging out and restarts.
- A `system.runInterval` (every 5 ticks) checks each player's selected hotbar slot for
  the tricorder and writes the actionbar line. It uses stable `@minecraft/server` only:
  `getComponent("inventory")`, `selectedSlotIndex`, `getViewDirection`,
  `onScreenDisplay.setActionBar`.
- **One target per player.** A new `guide_me` replaces the old one. "Dave, clear my
  tricorder" removes it. Targets are the player's own: a sibling's tricorder points
  where *their* Dave said.
- **Getting one:** Dave hands it over (`give` from the pack) the first time he sets a
  target for a player who has none. Optionally there is a crafting recipe too (owner's
  call).

**The item itself, in two steps** (§3c rule 2 says behavior pack only):
1. **v1, fully vanilla, no download:** an ordinary item renamed **"Tricorder"**, with
   lore ("Points where Dave says"). The script recognises it by its name tag and lore.
   Proposed item: a **spyglass**, because it has no needle of its own to contradict the
   arrow, unlike a compass. It can be lost, dropped or kept in a chest like any item.
2. **v2, a true custom item** (`jbrain:tricorder`, its own icon, stack size 1, can't be
   crafted by accident). That needs a **resource pack**, which BDS **pushes to every
   joining client** (`texturepacks-required=true`). Store clients accept server packs,
   and Marketplace servers rely on it, but it's a download prompt on join and it breaks
   §3c rule 2. **Owner decision when we get there**, after M0b checks the join prompt on
   the Xbox and Windows.

**Later, maybe:** Bedrock's **locator bar** (the `locatorbar` rule already exists on this
server) could show Dave's target as a real waypoint marker on screen. It's only worth
switching to once custom waypoints reach the stable script API, which M0b checks. Until
then the actionbar arrow is the vanilla path.

**Fair play:** the tricorder only points where Dave already said, so it's in the same
tier as the question that set the target (§3b). Settings that hide structure answers
also keep it from pointing at them.

**Tests:**
- Bearing to arrow at all eight octants and the wrap at ±180°.
- The arrival radius and the other-dimension text.
- The target survives a restart (dynamic property).
- A renamed item that isn't a tricorder (wrong lore) does nothing.
- Another player's target never shows on someone else's tricorder.

### P1–P3 — the owner's Minecraft agent: a persona you select, with maps in the chat

**One persona, two ways in (owner decision, 2026-10-10).** Minecraft_Dave is a single
persona. The in-game companion (M5/M6) and the PWA agent are the **same agent**: one
prompt and one set of goal, log and memory tools, reached through two doors. Each door
fixes **whose session it is**:

| Door | Who | Session player | Can switch player? | Tool tier |
|---|---|---|---|---|
| **PWA** (the agent picker, or "Ask about this world" on the Minecraft screen) | the owner | **the owner's gamertag** by default, a setting on the Minecraft screen | yes: a player picker in the chat, and "switch to Mira" | Owner: everything below |
| **In game** (`/jb:dave …`) | any player on the server | **that player**, from the xuid the server-side script reports (never typed) | **no** | Player: §3b's Player tier, plus their own goals, log and memory |

- **Each player has their own continuing conversation.** In game, a player's messages go
  to *their* Minecraft_Dave session, so "and the next one?" follows on. A session rolls
  over after 6 hours idle, keeping the history and starting a fresh context.
- **Per-player data follows the session player**, never the speaker's claim. A player's
  goals, log and memory are theirs, and in game nobody can read or write another
  player's (sharing between players is the owner's switch).
- **Why unifying is safe:** the persona has **no KB access**, so the in-game door can't
  reach the owner's notes whatever a player types. Player text is untrusted data,
  fenced, on every path.
- **In game, some owner tools are held back:**
  - the Admin tools stay with the gamertags on the admin list (§3b);
  - **web search and fetch are OFF in game by default**, an owner setting. They're for
    the owner in the PWA. Turned on in game, they'd let any player have the box fetch
    arbitrary pages and read them back into chat, which matters with children playing.
- The **in-game name** stays the companion setting (`/jb:dave`, "Dave"), and the persona
  shows as **Minecraft_Dave** in the PWA.

**Owner request (2026-10-10).** The owner wants a selectable agent persona for Minecraft,
with:
- maps rendered in the PWA as one of its tools;
- the **full Dave toolset**;
- **web search and fetch**;
- a **memory / session-log** toolset.

**Why it's a persona, not Dave.** Dave answers *players* in game, so he is deliberately
narrow: read-only, tier-gated, no web, no memory, no notes, and every word from a player
is untrusted. The owner's agent sits on the **owner side**. It is a new entry in
`jbrain.agent.agents`, alongside `curator`, `jerv`, `teacher` and `archivist` (ASSISTANT.md
§"Agent selection"), and it gets the owner tier of every tool. Both agents share the same
`mc_*` handlers; only the allowlist and the tier differ.

**The persona** (id `minecraft_dave`; display name **Minecraft_Dave**, owner decision
2026-10-10):
- **System prompt:** a Minecraft-savvy helper for the owner's server and family worlds.
  World data is the source of truth; game knowledge comes from looked-up data, not
  recall.
- **Tool allowlist:** a closed `frozenset` with the shape below, assembled by name like
  `note_ingest`'s.
- **KB access: none.** It doesn't read the owner's notes. That is the line against a
  confused deputy, since world text (signs, book contents, chat, gamertags) is
  player-authored and untrusted; see "Safety".
- **Selectable** in the existing agent picker, and openable from the Minecraft screen
  ("Ask about this world").

**Tools.** Everything in §3b at the **Owner** tier, plus three more groups:

| Group | Tools | Source / status |
|---|---|---|
| World, worldgen (works **today**, M0b) | `mc_locate_structure`, `mc_locate_biome`, `mc_world_info` (time, weather, day) | Console `execute positioned … locate`, already proven on the box |
| Server and play history (works **today**, M1) | `mc_server_status`, `mc_players` (totals, online, sessions), `mc_play_history` (who played when, joins and leaves by day) | `/api/minecraft` and `app.mc_player_sessions` |
| World index (after **M4**) | `mc_find_container`, `mc_find_villager`, `mc_find_block`, `mc_describe_area`, `mc_biome_at`, `mc_build_changes`, `mc_whats_new` | Snapshot index |
| Live (after **M5**) | `mc_nearest_entity`, `mc_where_is`, `mc_player_context`, `mc_where_did_i_die`, `mc_inventory` | Behavior-pack bridge |
| Maps (with **M8**) | `minecraft_map(center, radius, layers, overlays, slot?)`: terrain, biome, height, explored, changes, slice@Y; overlays for players, waypoints, structures, deaths | Renderer in the sidecar; returns an image artifact rendered **inline in the chat** |
| Admin (owner) | `mc_backup_now`, `mc_set_time`, `mc_set_weather`, `mc_announce` | Console allowlist; staged as Proposals per ASSISTANT.md's write policy, never silent |
| Web | `web_search`, `web_fetch` (the existing tools, same fences) | For wiki, recipe and seed questions the bundled data doesn't answer |
| Goals and progress log, **per player** | `mc_goal_create` / `mc_goals` / `mc_goal_update` (done, abandoned, rename), `mc_log` (add a progress entry, optionally against a goal), `mc_log_read` (a player's journal, filterable by goal or date) | `app.mc_goals` and `app.mc_goal_log`; see "Goals and the progress log" below |
| Memory, **per player**, self-managed | `mc_memory_read`, plus **line-level edits only**: `mc_memory_add(text)`, `mc_memory_replace(line, text)`, `mc_memory_remove(line)`. There is no whole-document write. | `app.mc_player_memory` (one row per line, with history); see "Memory: self-managed, never overwritten" below |

**Goals and the progress log (owner, 2026-10-10).** The "session log" is a **journal of
progress toward a player's goals**, and Dave helps that player get there. Everything is
tied to a **Minecraft player**, keyed by xuid like the play history, so a gamertag change
doesn't lose it.

- **Goals** (`app.mc_goals`): player xuid, world slot, title (for example "Beacon at the
  base" or "20 obsidian for the portal"), optional target notes, status
  (open / done / abandoned), created and finished times.
- **Progress log** (`app.mc_goal_log`): player xuid, optional goal, time, text, and
  **source**:
  - `owner`: written from the PWA agent;
  - `player`: typed in game, e.g. `/jb:dave log got 12 obsidian`;
  - `dave`: Dave's own summary entry, made only when the player asks;
  - later `auto` (M5): add-on events such as "died in the Nether", "crafted beacon".
- **Player memory** (`app.mc_player_memory`): short durable facts about one player. How
  it is written is below.

**Memory: self-managed, never overwritten (owner, 2026-10-10).** Minecraft_Dave keeps
its own memory, with no approval card per write, but it can never replace the whole
document. Two precedents in this repo set the design:

- **The archivist's clobber.** `archivist_memory_write` is a full-replace upsert. The
  archivist once rewrote its memory and then misreported what it had destroyed, so it
  now gets a before/after **receipt** (`agent/archivisttools.py`). The receipt only
  mitigates a full replace; it doesn't prevent one.
- **ASSISTANT.md's memory rule and `owner_prefs`.** The rule is "delta edits
  (ADD/UPDATE/REMOVE on individual bullets), never full rewrites — full regeneration
  rots accumulated self-knowledge". `owner_prefs` implements it: numbered lines, one
  line per call, and no full-rewrite verb.

So for each player:
- **Memory is numbered lines.** The only verbs are add, replace one line and remove one
  line. A single call can't wipe or rewrite the memory, the same guarantee `owner_prefs`
  gives.
- **Nothing is deleted outright.** A replaced or removed line keeps its old text with a
  `superseded_at` timestamp, so the owner can see a line's history in the player sheet
  and restore it. The agent reads only current lines.
- **Every write returns a receipt** quoting the line before and after (the archivist's
  lesson), so the model's account of its own memory stays grounded.
- **Caps**: 60 lines and 6k characters per player, checked before the write, with a
  refusal that says to consolidate.
- **Read at the start of a chat**: that player's memory and open goals are injected into
  the system prompt. Lines that came from player-typed text are fenced as data, because
  in-game logging (P2) means memory can carry untrusted words.
- In P2, **Dave in game can add memory lines for the asker only**, and only when the
  player asks ("remember my base is here"), which comes through the same verbs.

**How it's used:**
- **In game, through Dave** (player tier). The asker's identity comes from the
  **server-side script** (`origin.sourceEntity`), never from what the player types, so a
  player reads and writes **only their own** goals, log and memory.
  - `/jb:dave goals` lists them.
  - `/jb:dave log <text>` adds a progress entry.
  - `/jb:dave help with <goal>` makes Dave read the goal and recent log and answer with
    world data. For "20 obsidian": the nearest lava pool from `locate`, whether they own a
    diamond pickaxe from `mc_inventory`, and what's still needed.
  - Another player's goals stay private unless the owner turns sharing on, the same
    switch that governs `where_is`.
- **In the PWA, through the owner's persona.** A chat is **bound to one player**, chosen
  when it starts: a player picker, defaulting to the owner's own gamertag, which is a
  setting on the Minecraft screen. The persona reads and writes that player's goals, log
  and memory, and can switch players when asked. The owner sees every player's journal.
- **On the Minecraft screen.** Each player's sheet in Players gains a **Goals** list with
  the latest log lines. That is a small extension of the binding mock, and goes through
  the GUI gate only if it grows beyond a list.

**Rules for the data:**
- Player-typed entries are **untrusted text**. They are stored as data and shown to the
  model only inside the untrusted-data fence, never as instructions.
- Entries are capped (500 characters) and in-game writes are rate-limited per player.
- All three tables are **owner-only RLS**, with isolation tests. Dave's writes go through
  the api's owner context *on behalf of* the xuid the script vouched for; a player is
  never a database principal.

**Safety.**
- **Untrusted world text** (sign text, books, gamertags, chat) reaches the model only
  inside the `briefs.py` untrusted-data fence.
- **No reach into notes.** With web fetch in the allowlist and no KB access, there is no
  path from planted world text to the owner's notes.
- **Writes are staged.** Admin tools and memory writes follow the session's write policy,
  so a world-changing tool is a Proposal unless the owner allowed it.

**The map tool-view.** It's a new GUI surface, so it goes through the **GUI gate**:
three mocks and the owner's pick, as a registered component (DESIGN.md "Agent tool
views"). It shows the PNG with pinch-zoom, a legend for the layers, overlay toggles, and
tappable markers that show coordinates. Copying "go to X Z" is explicit. The Minecraft
screen's "Maps" section (M8) reuses the same component.

**Waves:**
- **P1 — the persona with what works today.**
  - The persona: its prompt, the picker entry, and launch from the Minecraft screen,
    with **the chat bound to one player**.
  - Worldgen `locate` and world info.
  - Server status, players and play history.
  - **Goals, the progress log and player memory**: their tables (with RLS tests), the
    tools, and the Goals list in the player sheet.
  - Web search and fetch.
  - The admin tools as Proposals.
  - Persona tests: the allowlist is closed, there's no KB access, and world text is
    fenced.
- **P2 — grows with M4/M5.** The index and live tools join the allowlist as each wave
  lands, one tool per PR, with tool-step-polish entries. With M5 the same goal, log and
  memory tools reach **Dave in game** at player tier (`/jb:dave goals|log|help with …`),
  scoped to the asker's own xuid. `auto` log entries come from add-on events.
- **P3 — maps.** M8's renderer, the `minecraft_map` tool, and the map tool-view (GUI
  gate), also embedded on the Minecraft screen.

**Decided (owner, 2026-10-10):**
- **No approvals** for goals, log or memory writes, **provided the session has a defined
  player**. A PWA chat with no gamertag set refuses the writes and asks for one, rather
  than guessing.
- Minecraft_Dave runs on the **local model**.

**Pending:** the owner's gamertag. It's entered on the Minecraft screen's settings, so P1
doesn't need it to be built.

## 3a. Debug control surface — the assistant as co-operator

The owner wants to hand the assistant a debug token and have it set up and debug the server,
with full control **of the game server**. This rides the existing debug console
(`../runbooks/DEBUG_ACCESS.md`): a capability token minted in **Settings → Debug access**,
time-boxed and revocable. A new route family follows the SDR precedent (`/debug/sdr/*`
probes exist because the owner has no terminal).

**What exists today**, before any Minecraft work:

- `POST /debug/update` and `GET /debug/update/status`: deploy `main` (this is what creates the
  container once the profile is on).
- `POST /debug/refresh`: rebuild a single service.
- `GET /debug/logs/{service}`, `GET /debug/host`, `GET /debug/disk`.
- `GET /debug/version`: confirm what's deployed.

There is **no** generic start/stop for a container on the debug router today. That is added
here for this one service only.

**New: `/api/debug/minecraft/*`**, proxied to the supervisor (lifecycle) and the wrapper
(everything else):

| Route | Does |
|---|---|
**Built in M0a:**

| Route | Does |
|---|---|
| `GET /minecraft` | The container as docker sees it, next to the game server as the wrapper sees it: state, BDS version, players, effective properties and snapshot count. |
| `POST /minecraft/{start,stop,restart}` | Supervisor lifecycle, for the `minecraft` container only. Restart is stop-then-start, because docker's own restart passes a fixed 10 s timeout that can kill the server mid-save. |
| `GET /minecraft/logs` | The BDS console as the wrapper recorded it, with sequence numbers. For a container that won't start, use `/debug/logs/minecraft` instead. |
| `POST /minecraft/console` `{command, wait_s}` | Runs one console command and returns what BDS printed in reply. **Game-scoped, not host-scoped.** Everything is allowed except `stop` and `save …`, which have the lifecycle and snapshot routes. There is no shell. |
| `POST /minecraft/snapshot`, `GET /minecraft/snapshots` | A hot `.mcworld` backup kept on the box. There is no download route: a world copy never leaves the box over the debug token, the same rule as `/debug/backup`. |
| `GET/PUT /minecraft/properties` | `server.properties` overrides, applied at the next restart. The ports are pinned. |

**Added by later waves:** worlds and slots (M2), restore (M3), and named probes such as a
snapshot parse or the bridge round-trip (M4, M5).

**Guards:**

- **Scope.** `minecraft.control` is listed in `/whoami`. As with every other debug scope,
  the list is informational: `DebugDep` is uniform, so any live token reaches these routes.
  The protection is the same as for the rest of the surface: tokens are time-boxed,
  revocable, and listed for the owner. (An earlier draft proposed a scope ticked at mint
  time; the debug surface has no per-token scopes, and adding them is its own change.)
- **Never host access.** The routes reach the supervisor's fixed command set and the wrapper's
  HTTP surface, never `docker exec` or a shell. "Full control" means full control of the game
  server, not of the box.
- **Audit.** Every write sets `debug_detail`, as the other debug routes do, so the owner's
  activity log shows what the assistant did to the server.
- **A `debug-connect.sh minecraft …` verb**, plus a row in the `DEBUG_ACCESS.md` route table in
  the same PR.

**Prerequisites and caveats:**

- Debug access must already be enabled on the box. That is a one-time host step
  (`DEBUG_ACCESS.md`, "Enabling it"), which is a known gap.
- The assistant reaches the box at the token's public host through the Cloudflare tunnel. A
  given assistant session's network might not reach it, so the first call is `GET /whoami`.
- The standing trade in `DEBUG_ACCESS.md` applies: a live token can also read personal data
  through the existing SQL and log routes. Mint it **short-lived** (1h or 24h) for a setup
  session, and revoke it after.

## 3b. Agent tool catalog (brainstorm, 2026-10-10)

These are the tools players reach **through Dave** in chat, and the owner reaches through the
PWA, jerv, and the debug API. Every tool is a handler over three sources: **live** (the
behavior pack's script, loaded chunks only), **index** (the last snapshot, M4), and
**worldgen** (`locate`, and the seed biome map from M8). Each answer names its source and
age. ★ marks the M6 core set; the rest land one tool per PR afterwards.

**Access tiers.** Each tier is an owner setting per slot, and is enforced in the tool registry,
never in the prompt:

| Tier | Who | What |
|---|---|---|
| **Player** | Anyone on the allowlist | Read-only world knowledge that a careful player could find out for themselves. |
| **Fair play** (a per-slot toggle, *off* = strict) | Same | Things that feel like cheating in survival: ore and X-ray finds, cave slices, the seed, ungenerated structures. Off in a survival world, on in a creative or "just exploring" world. |
| **Admin** | Gamertags the owner lists | Harmless game-state changes on request: back up now, set time or weather, clear weather. These are the only mutating tools, and each one is on the wrapper's console allowlist. |
| **Owner** | The PWA, jerv, and debug | Everything above, plus the server lifecycle, worlds, snapshots, and maps of any slot. |

**Find and navigate**

| Tool | Answers | Source | Tier |
|---|---|---|---|
| ★ `nearest_entity(type, filters)` | "nearest pig"; filters: baby, tamed, name tag, "my" (owned pets) | live → index | Player |
| `count_entities(type, area)` | "how many cows are in my farm?" | live → index | Player |
| ★ `locate_structure(kind)` | village, mansion, outpost, monument, ruined portal, ancient city, trial chamber, stronghold… | index → `locate` | Player for explored ones; **fair play** for `locate` beyond them |
| ★ `locate_biome(biome)` | "nearest cherry grove / mushroom island" | index → `locate` / seed map | Player |
| `find_villager(profession, trade?)` | "nearest librarian selling Mending" (villager trades are in the saved actor data) | index | Player |
| `find_block(block, area)` | "nearest diamonds", "any spawners near here" | index | **Fair play** |
| ★ `find_container(item)` | "which chest has my diamonds?" | index | Player |
| ★ `where_is(player)` | another player's position (sharing is on, owner decision) | live → index | Player |
| `guide_me(target)` / `stop_guiding` | a live **actionbar compass** ("→ pig, 42 m NE") refreshed every second until arrival. Display only: `titleraw … actionbar`, with no world change | live | Player |
| `portal_math(x,z)` | Overworld↔Nether coordinates; "where do I build the portal so it links to my base?" | computed | Player |
| `route(from, to)` | straight-line distance and bearing, plus a map with both points marked | computed + M8 | Player |

**About me and the world**

| Tool | Answers | Source | Tier |
|---|---|---|---|
| ★ `my_context()` | position, dimension, biome, facing, spawn or bed, last death | live | Player |
| `where_did_i_die(n?)` | the last *n* deaths, with cause and coordinates; "are my items still there?" (an item-entity check near the spot) | death events + live | Player |
| `my_inventory()` / `my_stats()` | "do I have enough iron for…?"; health, XP, hunger | live (script components) | Player |
| ★ `world_info()` | time and day count, ticks until night, weather, moon phase, difficulty | live | Player |
| `describe_area(radius)` | "what's around me?": biomes, water, structures, villages and players within *r*, as a short paragraph | index + seed map | Player |
| `biome_at(x,z)` | which biome is at a coordinate, explored or not | index → seed map | Player |
| `world_seed()` | the seed | `level.dat` | **Fair play** |

**Memory and history**

| Tool | Answers | Source | Tier |
|---|---|---|---|
| `waypoint_save(name)` / `list` / `delete` / `share` | "remember this as *home*"; "where's Josh's mine?" | `app.mc_waypoints` (per slot, per player) | Player |
| `whats_new(since?)` | "what happened while I was gone?": joins, deaths, achievements, big build changes | event log + snapshot diff | Player |
| `build_changes(area, since)` | "did anything change at the base since Tuesday?" A blocks-changed count plus a `changes` map (spots griefing too) | snapshot diff + M8 | Player |
| `playtime / leaderboard` | play time, deaths, distance travelled, per player | event log | Player |
| `remind_me(text, when)` | "remind me at nightfall to go home" (game time or real time), delivered in chat | scheduler + live | Player |

**Game knowledge** (looked up, not recalled from the model's memory):

| Tool | Answers | Source |
|---|---|---|
| `recipe(item)` | the crafting, smelting or brewing recipe | bundled Bedrock data files (PrismarineJS `minecraft-data`, Bedrock edition), pinned to the BDS version |
| `item_info(item)` / `mob_info(mob)` | drops, food values, spawn conditions, what a mob is weak to | same |

A weak local model "remembering" recipes is a known way to fabricate answers. Grounding them in
a data file is the point of these tools.

**Maps (M8)**

| Tool | Answers | Tier |
|---|---|---|
| `map(center?, radius, layers, overlays)` | a PNG share link (players) or an inline image (owner). "Map of my base", "show me the biomes within 1000 blocks", "where are all the villages?" | Player; `slice@Y` is **fair play** |
| `explored_map()` | what has been explored, as fog of war over the seed biome map | Player |

**Admin (gamertag list; mutating, each command on the console allowlist)**

| Tool | Does |
|---|---|
| `backup_now(label?)` | M3 snapshot: "Dave, back up before we blow this up" |
| `set_time(day/night/…)`, `set_weather(clear/rain)` | the obvious ones |
| `announce(text)` | a server-wide message |

**Owner-only, outside the game**: everything in §3a, plus jerv's `minecraft_world` (M4) and
`minecraft_map` (M8) tools, so the owner can ask jerv "show me the map of the Minecraft world"
from the PWA.

**Deliberately not tools**: giving items, teleporting, building, or editing blocks; reading
anything outside the active slot; anything that touches JBrain notes or the wiki; free-form
console access for players.

## 3c. Vanilla-client compatibility (owner requirement, 2026-10-10)

**Every player-facing feature must work on an unmodified, store-installed Bedrock client on
Windows and Xbox.** That rules out client mods, resource packs players install themselves,
and launcher tricks. The rules that keep it true:

1. **Everything runs on the server.** The behavior pack's script runs inside BDS. The client
   only ever gets standard protocol messages.
2. **Behavior pack only, no resource pack** for every planned feature. No custom textures,
   models, sounds or UI files. Anything that would need one (a custom NPC model) is labelled
   and left to the owner (M7).
3. **No experimental toggles.** Only stable script APIs. A feature that needs a beta API
   waits, or gets a stable fallback (§5).
4. **Output uses only vanilla channels.** Those are chat (`tellraw`), the actionbar and title
   (`titleraw`), sounds and particles the game already has, and server forms
   (`@minecraft/server-ui`), which the client draws from server data.
5. **No clickable links or in-game images.** Bedrock chat can't do either. Maps go out as a
   short code to type into a browser.

**Each player-facing feature, checked:**

| Feature | How the client sees it | Vanilla? |
|---|---|---|
| Joining (LAN list, `mc.hopkinsbrain.com`, relay address) | the standard server list | ✅ |
| Asking in chat (`Dave, …`) | ordinary chat, read by the server script | ✅ if M0 finds a stable chat event. Otherwise `/dave` |
| `/dave <question>` | a server-registered custom command, which appears in the client's normal command autocomplete | ✅ M0 checks it on Xbox and Windows |
| The Dave quick menu (M7, optional) | a server form | ✅ |
| Replies | `tellraw` chat, private to the asker | ✅ |
| `guide_me` compass | actionbar text, refreshed by the server | ✅ |
| Tricorder v1 (M9) | a renamed vanilla spyglass; actionbar arrow while held | ✅ |
| Tricorder v2 (M9, optional) | a custom item; needs a server-pushed resource pack | ⚠️ the owner chooses (download prompt on join) |
| Waypoint markers in the world (optional) | vanilla particles at a spot, visible only to the asker | ✅ |
| Reminders and warnings | chat or actionbar, plus a vanilla sound | ✅ |
| Maps | a short code typed into a browser; nothing is shown in-game | ✅ no in-game image |
| `my_inventory` / `my_stats` / deaths | read on the server by the script | ✅ |
| Admin tools (time, weather, backup) | ordinary server commands | ✅ |
| Companion NPC (M7) | a vanilla mob is fine; a custom model needs a resource pack | ⚠️ the owner chooses |
| Joining at all | the client must be on the **same version** as BDS | ⚠️ that is why M3's one-click update exists |

**M0 checks this on the real clients** with the fresh world, on the owner's Windows PC and on
the Xbox:

- What is shown on joining a server that has the behavior pack.
- Whether `/dave` shows up in autocomplete.
- Whether the actionbar compass updates smoothly.
- Whether `tellraw` reaches only the asker.

Any ❌ moves that feature to a vanilla fallback before M5 is scheduled.

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
- **Add-on**: yes. The companion is a behavior pack, which is an add-on. The vanilla client
  handles a server's packs on its own; at most a player sees the standard "download add-ons"
  prompt the first time they join, and M0 records exactly what Windows and Xbox show.
  Experimental toggles are still never turned on without asking.
- **Vanilla clients only (owner requirement, 2026-10-10)**: everything must work on an
  unmodified Windows or Xbox Bedrock client (§3c).
- **Input**: every player types, the Xboxes included, since they have keyboards. `/dave …`
  is enough, and the button menu is optional (M7).
- **Companion**: named **Dave** for now, and changeable later (M5, M6).
- **Player locations**: Dave may say where other players are.
- **Box backups**: Minecraft backups stay separate from the whole-box export (M3).
- **Maps and biomes**: the assistant and Dave can render 2D maps (terrain, biome, height,
  explored, changes) and answer biome questions, including beyond the explored edge (M8,
  §3b).
- **Testing**: a fresh, empty world first, on the home network only, with the assistant
  driving the box through a debug token (M0, §3a).
- **World slots**: several worlds live on the server, one loaded at a time. Each slot can be
  loaded, imported, created fresh, renamed, and **reset** (M2, M3).
- **World**: a small world on Windows, about 3 hours of building. It is imported by exporting
  a `.mcworld` (M2).
- **Backups**: on demand for now, plus automatic safety snapshots before risky actions (M3).
  Scheduled backups wait for M7.

**Still open:**

1. **Achievements**: M0 reports what the pack does to achievements on this world, for the
   record. The owner has already accepted the add-on.
2. **Per-player keep inventory: undecided (owner, 2026-10-10).** Bedrock's `keepInventory`
   is a world-wide game rule, so a per-player version needs the M5 add-on. The approach
   worked out, recorded for if it's chosen:
   - Turn `keepInventory` on for the whole world. When a player who is **not** on the keep
     list dies, the script drops their inventory, armor and off-hand at the death spot,
     destroys Curse of Vanishing items, and resets their XP and drops roughly the vanilla
     amount as orbs.
   - That direction means a failed or disabled add-on leaves everyone keeping their items,
     never losing them. The opposite (rule off, restore items on respawn) risks loss or
     duplication around disconnects and restarts.
   - It is owner-only, per player and per world, set from the Players section, never by
     players through Dave.
   - If it's chosen, M0 or M5 first checks that the inventory is still readable at
     `entityDie` and that `spawnItem` works on stable APIs.

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
