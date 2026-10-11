# Minecraft Bedrock — an on-box world server, its backups, and a companion that knows the world

> **Status:** In progress · **Last verified:** 2026-10-10 · **Waves:** M0◻️ M1✅ T1✅ M2✅ M3✅ M4◻️ M5◻️ M6◻️ M7◻️ M8◻️ M8a◻️ M9◻️ M10◻️ M11◻️ M12◻️ M13◻️ M14◻️ R1◻️ P1✅ P2◻️ P3◻️

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
  - **The home-test kit is ready** (`../runbooks/MINECRAFT_HOME_TEST.md`): the probe
    packs now carry the custom items, the recipe and a server-pushed resource pack, and
    the runbook lists the eight checks in one sitting.
  - **Added for M9/M10:** the server-pushed resource pack. On joining from the Xbox and
    from Windows, the pack downloads automatically with no install step, a test Power
    Pack shows its icon and name, and the nine-eye recipe works in a crafting table.
    Also time the join: the extra download should add almost nothing.

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
- A visible **companion NPC**. A *custom* entity's model and texture ride in the `jbrain`
  resource pack that downloads automatically on join (allowed since 2026-10-10, §3c), at
  the cost of a bigger download. The lighter option is a **vanilla mob** (an allay or
  villager named "Dave", made invulnerable by the pack). Which to use is the owner's call. Spawning it
  changes the world, so it is an owner action, never a player's.
- Scheduled backups: a workflow-scheduler entry that calls M3's on-demand route.

### T1 — Travel log: per-player trails and fog of war (owner, 2026-10-10)

**Shipped (code):**
- The wrapper samples each online player with `querytarget` every 10 s (kept after 4
  blocks of movement) and serves the samples at `GET /track`.
- The `jbrain` behavior pack (`deploy/minecraft/jbrain-pack/`) is installed into
  whichever world starts, and logs deaths (with cause and killer) and respawns.
- Migration 0225 adds `mc_player_track`, `mc_player_explored` and `mc_player_events`,
  owner-only, with isolation tests. The api's `TrackDrain` and the session drain fill
  them.
- A reset or import deletes that world's history (`world_replaced`).
- `GET /api/debug/minecraft/travel` shows the counts.
- **`querytarget` checked on the box (2026-10-10):** an armor stand was summoned at spawn
  under a temporary ticking area, then removed.
  - On 1.26.52.3 the reply is **pretty-printed over many lines** (`Target data: [` … `]`),
    not one line, so the parser re-joins it. The first deploy's one-line parser would
    have dropped every sample.
  - The fix carries the captured reply as its test.
- **Still M0b, with a player online:** a real death line arriving with its cause.

**Start recording early, draw it later.** History can't be recorded after the fact, so
the log starts as soon as it's cheap to (right after M2/M3). M8 draws it.

- **Sampling (no add-on needed):** while anyone is online, the wrapper runs
  `querytarget "<name>"` on the console for each online player (names come from the join
  events) every **10 s**. It's a vanilla command, and BDS answers with JSON: dimension,
  position and facing.
  - A sample less than 4 blocks from that player's last kept one is dropped, so standing
    still costs nothing.
  - **M0b check:** the reply's exact shape on 1.26, and that it costs the server nothing
    noticeable.
- **Storage (owner-only RLS, with an isolation test, like `mc_player_sessions`):**
  - **`app.mc_player_track`** holds the trails: world folder, xuid, time, dimension,
    x/y/z. About 4 players × 6 a minute while moving; months of play is a few MB.
  - **`app.mc_player_explored`** holds the per-person fog of war: world, xuid, dimension,
    chunk x/z, first seen, last seen. A chunk is revealed when a player comes within **4
    chunks** of it, which is roughly what they could see.
  - Both are drained from the wrapper's events with the boot-id and replay-safe pattern
    of play sessions.
- **What it gives:**
  - **M8 map layers:** *explored by* (one player, or everyone, each in their own
    colour), *trail* (a session's path, or a day's), and *heat* (where time was spent).
  - **Dave and the agent:** "where was I an hour ago?", "how did I get to that village?",
    "has anyone been east of the river?" (the M6 `my_track` and `explored_by` tools).
  - **PWA:** each player's lifetime stats gain distance travelled per dimension.
- **A world reset or a restore:** a world's track and fog follow its **folder**. A reset
  or "new seed" clears them, because they describe terrain that no longer exists. A
  restore keeps them, since some of the trail may now be "in the future" of the restored
  world, and that's harmless.
- **Privacy:** family-only and owner-visible, as decided for "where is everyone"
  (2026-10-10). Players can ask Dave about their own trail. Seeing others' trails follows
  the same rule as "where is Sam".
- **Events on the timeline (owner, 2026-10-10)**, in **`app.mc_player_events`**: world,
  xuid, time, kind, dimension, x/y/z, detail. Kinds:
  - **joined / left:** from the play sessions already recorded (M1), placed at the
    session's first and last sample.
  - **died:** BDS prints no death line on the console, so this needs the **first slice of
    the `jbrain` behavior pack**. It is a `world.afterEvents.entityDie` handler for
    players that `console.log`s the place and the cause (`damageSource.cause`, plus the
    killer's type if any), using the bridge M0 proved. The wrapper installs it into the
    loaded world, as it does the probe pack. M5 later grows the same pack.
    - **M0b check:** the death line arrives, with its cause, from a real death.
  - **respawned:** from the same pack (`playerSpawn`, when it isn't the first spawn).
    Together with died, it gives the death-to-respawn gap.
  - **changed dimension:** derived from the trail. No extra source is needed.
- **Size:** about 150 lines of wrapper, the pack's death handler, one migration (track,
  explored and events), a drain and tests. It's still one PR.

### M8a — Timeline: scrub through time on the map (owner, 2026-10-10)

**What the owner sees.** The PWA map gets a **time scrubber** along its bottom edge:
- **Scrub or play** at 1×, 10×, 60× or 600×. Each player is a coloured dot that moves
  along their trail, with a fading tail of the last few minutes behind it.
  - Positions between 10-second samples are interpolated.
  - A dimension change jumps the view to that dimension's map, with a note.
- **Events** are marked on the scrubber and pinned on the map at their spot: deaths as a
  skull, with the cause on tap ("fell from a high place", "killed by a Creeper"), plus
  log-ins, log-outs and dimension changes. Tapping a marker on the scrubber jumps there.
- **Player chips** choose who's shown. **Follow** keeps the map centred on one player.
- **Sessions:** with a player chosen, **‹ previous session · next session ›** steps
  through their play sessions. Each shows its date, length and distance. Jumping lands on
  the session's start, and the scrubber zooms to fit it.
- **The fog reveals over time:** the explored layer shows only what had been seen **by the
  scrubbed moment** (each chunk's `first_seen` ≤ t), so you watch the world open up.
- **Range presets:** this session, today, this week, everything. "Now" snaps back to live
  positions.

**How it's built:**
- **API (owner-only):** `GET /api/minecraft/timeline?world=&from=&to=&players=` returns
  the trails, downsampled to the zoom level (no more than about 2,000 points a player),
  plus the events and the session list.
  - The explored layer comes with first-seen times so the client can fade it in.
  - The map tiles themselves are M8's.
- **The client** keeps the trails in memory for the range on screen. Scrubbing is a
  local redraw with no request per frame, so it stays smooth on a phone.
- **Phone first:** a full-width scrubber with large handles, event markers with 44 px tap
  targets, and play and pause in the thumb zone.
- **GUI gate first** (DESIGN.md process): mocks of the scrubber and the session stepper
  before any of it is built.

**Depends on:** T1, which must already have been recording, and M8's map tiles.

### M8 — Maps and biomes (after M4; the map tools in §3b)

**Renderer, first layer (shipped 2026-10-10).**
- **Reader:** `deploy/minecraft/leveldb.py` is a stdlib reader for Mojang's LevelDB
  (tables with raw-deflate blocks, plus the write-ahead log, newest write winning). It
  never writes or locks, so it reads a world while BDS runs.
- **Biome layer:** `mapping.py` renders the **biome atlas** from each chunk's `Data3D`
  record (heights and biomes, no block parsing), hillshaded, with ungenerated ground
  transparent (fog).
- **Tiles:** 256×256 px. Zoom 0 is 1 px per block; zoom k is 1 px per 2^k blocks, up
  to zoom 4 = **1 px per chunk** (owner, 2026-10-10), a 4096-block tile. Zoomed-out
  pixels read only the one column they show, so every zoom renders in milliseconds. A
  per-world chunk index and rendered tiles are both cached until the world's files
  change, re-read at most once a minute: a running server's log moves every few
  seconds, so the map trails a live world by up to a minute (issue #1608).
- **No holes where players have been** (owner, 2026-10-10: "darker pixels are fine,
  missing is not"). Transparent means never generated, nothing else. A chunk last saved
  before 1.18 has only the older `Data2D` record (heights from y=0, one-byte biomes), so
  the renderer reads that too, lifted onto the 1.18 floor so it meets its neighbours
  without a seam; a re-saved chunk's `Data3D` wins. Not yet checked against a real
  pre-1.18 world: the layout is the documented one, which `Data3D` shares.
- **Satellite layer: ground nobody has visited** (owner, 2026-10-11: "like a satellite
  from orbit would have seen"). Since 1.18 Bedrock generates biomes and terrain from the
  same noise as Java for the same seed, so `jbrain-predict` (`deploy/minecraft/predict/`,
  built against cubiomes, MIT, at a pinned commit in the image's build stage) predicts
  the Overworld's surface from the seed in `level.dat`, and the real chunks are drawn
  over it. **Explored vs unexplored stays visible** (owner, same day): predicted ground
  is drawn as the satellite's *survey* — half-desaturated, at 65% brightness — and
  explored ground in full colour, so the whole world pans but what anyone has actually
  seen stands out. **Lore:** the server's AI connection comes from an AI satellite in
  orbit; the survey is its view. Minecraft_Dave can speak from that later.

  Checked against a 1.26 server generating the same seed:
  - biomes match at **97%** of 4,311 real columns, and every miss sits on a border
    between two biomes;
  - heights are off by a median of about 2.5 blocks (90% of points within 9, trees
    included);
  - cubiomes speaks Java's ids; the 1.13+ oceans and every 1.16+ biome are translated
    (`JAVA_TO_BEDROCK`), each checked on a real spot;
  - the biome is read AT the predicted surface (the height map's own ids came from
    below it: a cave biome under a beach).

  **The Nether did not match** (3 of 4 spots wrong), so only the Overworld is predicted:
  ungenerated Nether and End ground stays transparent. `map/info` says which
  (`predicted`). Predicting them is being researched (a Bedrock-specific Nether
  generator, the End's parity with Java, or the server's own `locate biome`).

  **Structures can't come from cubiomes**: Bedrock places them differently from Java.
  The server's own `locate structure` does it exactly instead. It is instant, read-only
  (it generates nothing), works in the Nether, and answers nearest-from-a-point, so a
  grid sweep finds every one in an area once per world. That is the next layer:
  structure markers.
- **Routes:** `GET /api/minecraft/map/info` and `/map/tile/{dim}/{z}/{x}/{y}.png`
  (owner), plus debug twins.
- **Facts verified on a real 1.26 world** that BDS generated locally:
  - the height map is indexed `z*16 + x`. Chunk-border smoothness picked the order:
    0.53 against 3.84 for the transpose;
  - heights are measured above the floor (−64);
  - biome stores are bottom-up, one per 16-high section, in x,z,y order.
- **Next:** the true-colour surface layer (top block from `SubChunk` records), the
  overlays from the travel log, and the viewer itself (GUI gate:
  `docs/mocks/minecraft-map/`).

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
- **It's personal (owner, 2026-10-10).** It is named **"<Player>'s Tricorder"** (e.g.
  "Steve42's Tricorder") and points to **its owner's** target, whoever holds it, so a
  sibling can carry it to lead the way. The owner's id is an item dynamic property (the
  item doesn't stack, so it can hold one).
  - Each player has one at a time. A replacement from Dave retires the old one, which
    then reads "This tricorder is retired".
- **Never dropped on death (owner, 2026-10-10).** The item is created with
  `ItemStack.keepOnDeath = true` (stable API), so it stays in the inventory through a
  death even when everything else drops. M10's drop path skips it too.
  - It can still be thrown away, put in a chest, or burnt in lava like any item. Dave
    replaces a lost one on request.

**The item itself: a true custom item, `jbrain:tricorder`** (owner, 2026-10-10: a small
pack that downloads automatically on join is fine, §3c rule 2).
- **Its own icon** comes from the server-pushed `jbrain` resource pack (§3c rule 2).
- Stack size 1. Lore: "Points where Dave says". It can't be crafted by players.
- The game's own `minecraft:display_name` is "Tricorder". Each copy's name tag is set to
  **"<Player>'s Tricorder"** when Dave gives it.
- It's never confused with a real item, because the script recognises the item type.
  Name and lore are not needed for that.
- **Fallback if the join download fails on a client in M0b:** a spyglass renamed
  "<Player>'s Tricorder" with lore, recognised by its lore. It has the same behaviour
  without its own icon.

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

### M10 — Power Packs: keep your inventory, or teleport (owner, 2026-10-10)

#### The Power Pack (the currency)

A **Power Pack** is crafted from **a full crafting table of Eyes of Ender**: nine eyes in a
3×3 grid make one pack. It looks like an Eye of Ender but carries its own name. That
makes it deliberately hard to get, at nine Ender Pearls plus nine Blaze Powder each.
Plain Eyes of Ender do nothing special; they stay the End-portal key and nothing else.

**How it's made:**
- A **custom item `jbrain:power_pack`**:
  - Name "Power Pack", plus lore ("Keeps your things, or takes you somewhere").
  - Max stack 16.
  - **Its own icon**, an Eye of Ender look with a glow so it's told apart at a glance,
    from the server-pushed `jbrain` resource pack (§3c rule 2).
  - It isn't an ender eye to the game, so it can't be thrown or used to fill an End-portal
    frame by accident.
- A **shaped recipe** (`recipes/power_pack.json`, crafting table, nine
  `minecraft:ender_eye` → one `jbrain:power_pack`) in the behavior pack. Recipes are
  stable data and need no experiment, and it shows in the recipe book like any vanilla
  recipe.
- **Checked in M0b:** the item shows with its icon and name, and the recipe works, on the
  Xbox and on Windows, after the automatic download on join.
- **Fallback, only if that download fails on a client:** Dave "forges" a pack instead of
  the crafting table. "Dave, make me a Power Pack" takes nine eyes and gives an Eye of
  Ender named "Power Pack" with lore. The script recognises it by its lore (an anvil can
  rename an item but can't add lore, so it can't be faked).

#### Keep your inventory, once per Power Pack

**The rule.** A player who dies with a **Power Pack** anywhere in their inventory keeps
everything, and **one pack is used up**. Without one, the death is a normal Survival death:
items and XP drop at the spot. The tricorder (M9) is kept either way.

**This decides "Per-player keep inventory" (§4).** It uses the fail-safe direction worked
out there:
- **The world's `keepInventory` rule is ON**, and the pack **drops** the inventory of a
  player who had no Power Pack.
- If the pack fails or is turned off, everyone keeps their things. Nobody ever loses
  items to a script fault, and there is no duplication around disconnects or restarts.
  The other way round (rule off, restore on respawn) risks both.

**On `world.afterEvents.entityDie` for a player** (stable API):
- **Pack present:** remove one `jbrain:power_pack`, taken from the smallest stack first.
  Keep the rest, which the `keepInventory` rule already does.
  - On respawn they get a private chat line and a vanilla sound: `Your Power Pack
    burnt out — you kept your things. 2 packs left.`
- **No pack:** for the inventory, armor and off-hand, `spawnItem` each stack at the death
  spot and clear the slot. Skip anything with `keepOnDeath` (the tricorder). Destroy
  **Curse of Vanishing** items.
  - Then reset their XP and drop roughly the vanilla amount as orbs. That matches a
    normal death, which only a pack avoids.
- **Died in the void:** a drop would fall out of the world, so the drop goes to the
  player's last safe position instead. This is a deliberate kindness, and settable later.

**Per world, owner-only.** A switch on each world's page: **"Power Pack keeps
inventory"**.
- Turning it on sets that world's `keepInventory` rule on and installs the pack's charm.
- In the rules editor, `keepInventory` then shows **"managed by the Power Pack charm"**
  rather than a free switch, so the two can't contradict each other.
- With the switch off, the world behaves exactly like vanilla.

**Why it's costly:** one death saved costs nine eyes, so it's a real decision, not a free
safety net (owner, 2026-10-10).

**Checks first (M0b, needs a player):**
- With `keepInventory` on, the inventory is readable and writable at `entityDie`.
- `spawnItem` keeps enchantments, names and durability.
- XP can be read and reset there.

**Tests:**
- Pack present: one consumed, everything else kept, the tricorder kept.
- No pack: everything dropped at the spot except the tricorder, vanishing items gone.
- Two deaths with one pack: the first keeps, the second drops.
- A pack in the off-hand or a shulker box: the off-hand counts; inside a shulker box it
  does not (the rule is "in your inventory").
- Pack disabled: everyone keeps (fail-safe).
- Nine eyes in the crafting table make one Power Pack; eight make nothing.

#### Teleport to a known place, one Power Pack per trip (owner, 2026-10-10)

**The rule.** A player who has settled a place with Dave can ask to be **teleported there**.
It costs **one Power Pack** from their inventory. With none, Dave says so, and offers
the tricorder (M9) instead.

**"Known" means a coordinate that has been pinned down, not a guess.** These count:
- **Saved places:** `waypoint_save` ("Dave, remember this as *home*"), set while standing
  there or confirmed in conversation with Dave ("the village at 410, -1220 — save it as
  *market*").
- **Places a player has stood:** their bed or spawn, and the waypoints others have shared
  with them.
- **A located structure or biome** (`locate`) counts only after Dave has stated the
  coordinate and the player has confirmed it ("yes, that one"). That is the "solidify"
  step. Until then it's tricorder-only.

**The flow (in game):**
1. "Dave, take me to *market*." Dave names the place, the distance and dimension, and the
   cost: `Market — 1,240 blocks, Overworld. Use 1 Power Pack? (you have 3)`.
2. The player confirms on a **server form** with Yes and No buttons (`@minecraft/server-ui`,
   vanilla), so a misheard place never costs a pack.
3. The script checks the Power Pack is still there, then calls `player.tryTeleport(spot,
   {dimension, checkForBlocks: true})` (stable). **Only if the teleport succeeds** is one
   Power Pack removed. A blocked or failed teleport costs nothing, and Dave says why.
4. It plays the vanilla enderman-teleport sound and portal particles at both ends.

**Safety:**
- **Landing spot:** the saved Y is used, and the two blocks above it must be free
  (`checkForBlocks`). A spot that has since been built over is refused, not glitched into.
- A place in **unexplored (ungenerated) terrain** has no safe Y yet, so it stays
  tricorder-only until someone has walked there.
- **Cross-dimension** trips are allowed only to places in a dimension the player has
  already visited, so nobody gets into the End early.
- **Never to another player** by default. "Take me to Sam" would need Sam to accept on a
  form, and that is left as an owner option for later.
- Every trip is logged (who, from, to, pack spent) in the session log the owner sees.

**Per world, owner-only switch:** "Power Pack teleport" (on by default, alongside the
keep-inventory charm). With it off, Dave says teleporting isn't allowed in this world.

**Why it stays fair:** each trip costs a Power Pack (nine eyes). The same packs are the
keep-inventory charm, so players choose how to spend them.

**Tests:**
- No pack: refused, nothing changes.
- Blocked landing: refused, and the pack is kept.
- Success: exactly one Power Pack removed and the player is at the spot.
- An unconfirmed `locate` result is refused as unknown.
- A cross-dimension trip to a dimension never visited is refused.
- Choosing No on the form costs nothing.

### M11 — Trans-dimensional chests (owner idea, 2026-10-10; details open)

**The idea.** An expanded ender chest. A **trans-dimensional chest** can be **dyed a
colour**, and all of one player's chests of the same colour share **one inventory**, in
any dimension. So a red chest at the base and a red chest in the Nether hold the same
things, and a blue pair is a separate store. This is late-game ("End") content: crafted,
not handed out.

**Two kinds (owner, 2026-10-10):**

| | **Private** trans-dimensional chest | **Cargo** trans-dimensional chest (hopperable) |
|---|---|---|
| Whose store | **The opener's**: like a vanilla ender chest, everyone sees their own red store in any red chest | **The placer's**: a red cargo chest moves items to the placer's other red cargo chests |
| Hoppers | **No**, like a vanilla ender chest. A hopper under it would drain someone's private store | **Yes**, hoppers feed in and pull out on both ends |
| What it's for | Carrying your own things between bases and dimensions | Automation: a farm in one place feeding storage at the base, across dimensions |
| How it works | The travelling vault (below) | An item pipe (below) |

The private chest is the design below. The cargo chest follows it.

**The engineering problem, and the proposed answer (private chest).**
- **The problem:** a stable-API script can't copy an item's full data (enchantments,
  names, durability, shulker contents) into storage and back. Syncing a "shared"
  inventory between several chests risks losing items or duplicating them, especially
  when two are open at once.
- **The answer: one vault per channel, which travels.** Each channel (player + colour)
  is a single invisible **vault entity** with a `minecraft:inventory` component. Entities
  with inventories keep their items through any teleport, including across dimensions.
  - At rest it waits in a fixed **vault room**, a small area kept loaded with a
    `tickingarea`, out of reach of players.
  - Opening any chest of that colour **moves the vault entity into that chest**, and the
    player opens its inventory as they would a chest minecart's.
  - When the player walks away or closes it, the vault returns to the vault room.
  - **Items are never copied, only the vault moves**, so nothing can be duplicated or
    lost. A chest of a channel whose vault is out (open somewhere else) says "In use at
    the other red chest".
- **The private chest itself** is a custom block, `jbrain:td_chest`, with a `color` state. Its
  model and colours come in the join-download resource pack (§3c rule 2).
  - **Dyeing:** use a dye on it (`playerInteractWithBlock`) to set the colour, which
    switches the chest to that colour's channel. A channel's items stay in its vault, so
    re-dyeing a chest never moves or loses anything.
  - Breaking the chest drops only the chest; the contents stay in the vault.
- **Spike first (an M0-style check, needs a player):**
  - an entity inventory opens from the Xbox and from Windows;
  - a vault entity keeps a full inventory (enchanted and named items, a filled shulker
    box) through a cross-dimension teleport;
  - the tickingarea vault room survives a server restart;
  - the interaction events this needs are on stable.

**The cargo chest: an item pipe between chests of one colour.**
- **Why it can't share one inventory:** hoppers act on real containers, in many places
  at once, while a travelling vault can be in only one place. So a cargo chest is a real
  container, and its colour makes a **pipe**.
- **One receiver per colour (owner, 2026-10-10):** the **first** red cargo chest a player
  places is their red **receiver**. Every red cargo chest they place after it is a
  **sender**. Items arriving in a sender, by hopper or by hand, are moved to that one
  receiver, and a hopper under the receiver pulls them onward.
  - There's no switch to flip. Each chest shows an in or out mark, and its name says
    which it is ("Steve42's red cargo receiver").
  - **Receiver broken:** that colour has no receiver, and its senders hold their items
    until one exists. The **next red cargo chest that player places** becomes the
    receiver. An existing sender is never silently promoted, so items never start
    piling up somewhere unexpected.
  - **Re-dyeing follows the same rule:** a cargo chest dyed to a new colour becomes that
    colour's receiver only if the colour has none, and otherwise it's a sender. Its
    contents stay in it either way.
- **Moves are real `Container.moveItem` calls**, stable, run inside one script tick.
  Items keep all their data and can't be duplicated.
- **Back-pressure:** when the receiver is full, items simply wait in the sender, just
  like a full hopper chain. Nothing is dropped or destroyed.
- **Loading:** the sender is loaded because its hopper is working. A receiver in an
  unloaded area (another dimension, a far base) is kept loaded with a `tickingarea`.
  - There's one receiver per player per colour, so one ticking area each. BDS limits how
    many ticking areas there are, so each player gets a small number of **active
    colours** (for example 4).
  - That limit is also a natural cost lever.
- **The container:** a custom block with an inventory, if the M11 spike shows custom
  blocks can hold one on stable and hoppers see it. Otherwise it's a vanilla barrel the
  script registers by position, marked with a coloured particle and a name, which
  hoppers already handle.
- **Who can use it:** anyone can put items into a sender, which makes it a public drop
  box. Only the placer can re-colour it, and breaking it drops its contents like
  a normal chest.
- **Spike additions:** hopper interaction with the chosen container; `moveItem` keeping
  enchanted, named and shulker items across dimensions; how many ticking areas BDS
  allows.

**Open (owner):**
1. **Cargo sharing:** can a cargo network be shared between players (Sam's red sender
   feeds Josh's receiver), or is it only ever the placer's? Routing is decided: one
   receiver, the first one placed.
2. **Cost.** For example, crafting one chest needs an ender chest plus a Power Pack
   (nine eyes), and the dye is free. Or the chest is cheap but each **new colour channel**
   costs a Power Pack the first time it's used.
3. **How many colours:** the 16 dye colours.
4. **What happens to a vault if its owner is removed from the allowlist:** it's kept, and
   the owner can see its contents from the PWA.

### M12 — Wormhole gates: two linked portals (owner idea, 2026-10-10; details open)

**The idea.** Like a Nether portal, but between **two places the players choose**, in any
dimensions. Gates are **very expensive** to make.

**How a gate comes to be (proposed):**
1. **Craft a pair of Wormhole Seeds.** One craft makes **two** linked seeds
   (`jbrain:wormhole_seed`, stack size 1). They share a pair id stored on each seed as an
   item dynamic property. The tooltip says "Wormhole Seed — pair 7, side A/B".
2. **Build a frame and plant seed A**, as with a Nether portal.
   - The player builds the frame first: e.g. a 4×5 ring of **crying obsidian**, which is
     itself costly.
   - Using the seed on the frame's base plants it, and the script checks the frame's
     shape.
   - The gate **forms but stays dormant**: a dim, still surface with a faint particle
     shimmer, and a chat line "Waiting for its twin".
3. **Plant seed B anywhere else**, in any dimension. **Both gates come alive** at the same
   moment, with an animated wormhole surface and a vanilla sound at both ends. The link
   is **bi-directional**.
4. **Stepping in:** the script sees a player inside the gate surface and
   `tryTeleport`s them to the other gate's exit spot, facing out. A short cooldown stops
   the player bouncing straight back. Mobs and items don't travel; that's a later option.

**Rules (proposed):**
- **Breaking any frame block** takes that gate down, and the twin goes dormant again.
  Rebuilding the frame and re-using the seed restores it. Mining the gate's core returns
  the seed, so a gate can be moved.
- **A blocked exit** (built over, or filled with water or lava) refuses the trip rather
  than putting the player inside blocks.
- **Logging:** every trip is recorded (who, from, to), and gates show on the M8 map.
- **A cross-dimension gate** to a dimension a player has never visited is refused, so
  nobody reaches the End early. This is the same rule as the M10 teleport.
- **Gates are shared infrastructure.** Anyone can step through, unless the owner makes a
  gate private to its builder (owner option).

**Built on:**
- custom items and a custom block for the gate surface (its texture and animation come in
  the join download);
- frame detection by block checks;
- `tryTeleport`;
- gate records kept by the pack: world dynamic properties for pair id, both ends, state
  and builder, mirrored to the sidecar for the PWA.

It needs M5's pack install, not Dave, though Dave can answer "where does the blue gate go?"

**Open (owner): how expensive?** Some options:
1. **A Nether Star at the core:** a seed pair needs a Nether Star (killing a Wither) plus 4
   Power Packs (36 eyes). This is the true end-game, and only a few gates ever exist.
2. **Power Packs only:** 8 Power Packs (72 eyes) for a pair. That's heavy grinding, but
   no boss fight.
3. **A cheaper seed with an expensive frame:** the frame needs crying obsidian plus a
   ring of blocks of diamond or netherite. A gate is a visible monument of what it cost.

Also open:
- whether a gate can be re-linked;
- whether more than two gates can share a network (a hub);
- whether mobs and items travel.

### M13 — The laser cannon: a beam weapon (owner idea, 2026-10-10; details open)

**Yes, it's doable on stable APIs, with nothing for players to install** beyond the join
download (§3c rule 2). A beam is a **hitscan**: an instant line, not a flying projectile.
- **The item:** `jbrain:laser_cannon`, a custom item with its own icon from the join
  download. It's held like a tool, with a cooldown so it can't machine-gun.
- **Firing** (`world.afterEvents.itemUse`):
  - The script traces the player's aim with `getEntitiesFromViewDirection` and
    `getBlockFromViewDirection` (both stable, up to e.g. 48 blocks).
  - It hits the **first** mob in the line, unless a block is nearer, which stops the
    beam.
  - Damage is applied with `entity.applyDamage(n, {cause, damagingEntity: player})`, so
    kills count as the player's and mobs drop loot as normal.
- **The beam:** a line of particles from the muzzle to the hit, drawn in the same tick
  with `dimension.spawnParticle`. It uses a red beam particle and a zap sound from the
  join download, with vanilla particles as the fallback. There's a small spark burst at
  the hit.
- **Charge shot (optional):** `itemStartUse` and `itemReleaseUse` (stable). Holding
  charges the shot (shown on the actionbar), and letting go fires a stronger, wider beam.
- **Rules it obeys:**
  - **Players** are only hit when the world's `pvp` rule is on. Otherwise the beam passes
    through them.
  - **Blocks are never broken** by default. An owner option allows lighting fires or
    breaking soft blocks, and it obeys `mobGriefing`.
  - **No hits through walls:** the block trace stops the beam.
- **Spike first:** the hit and particle timing on Xbox, that `applyDamage` credits kills
  and loot, and the cost of drawing the particle line with 4 players firing.

**Open (owner):**
1. **Ammo or energy:** Power Packs (M10, for example one pack = 20 shots, on a charge
   meter), or Redstone, or no ammo but a long cooldown.
2. **How strong:** roughly a diamond sword (7 damage) per shot, or a bow-and-arrow-ish 4,
   with the charge shot up to double.
3. **The recipe:** for example a Power Pack + a Beacon? + iron and redstone. It's
   end-game like the gates, or mid-game.
4. **PvP at all**, even on worlds where `pvp` is on (kids' worlds)?

### M14 — Powered armor: netherite plus a Power Pack (owner idea, 2026-10-10; details open)

**The recipe (owner):** a **netherite armor piece + a Power Pack** gives the **powered**
piece: powered helmet, chestplate, leggings and boots.
- **Made at the smithing table, not the crafting table.** Bedrock's
  `recipe_smithing_transform` (behavior-pack data, stable) is how vanilla upgrades diamond
  to netherite, and it **keeps the piece's enchantments and damage**. A crafting-table
  recipe would wipe a player's enchantments.
- **The slots:** template = netherite upgrade smithing template, base = the netherite
  piece, addition = a Power Pack. A full set costs 4 Power Packs (36 Eyes of Ender) plus 4
  templates on top of netherite. That's end-game.
  - **Option:** make the Power Pack itself the template (tagged
    `minecraft:transform_templates`), so no netherite template is needed. It's cheaper,
    and the owner chooses.
- **The items:** `jbrain:powered_helmet` and the rest. They're custom wearables with
  netherite-level protection and toughness and fire-proof, plus the powers below. The
  icons and the worn look (attachables) come in the join download (§3c rule 2).

**Powers (proposed; the owner picks):** a script checks worn armor every second
(`getComponent("equippable")`, stable) and applies effects (`addEffect`, stable) while a
piece is worn.

| Piece | Power while worn |
|---|---|
| Helmet | Night Vision, and water breathing |
| Chestplate | Resistance I |
| Leggings | Speed I |
| Boots | No fall damage (cancelled from `entityHurt`), and Jump Boost I |
| **Full set bonus** | **Fire Resistance**, and a faint glow on the HUD: "Powered armor: online" |

**Open (owner):**
1. **Which powers:** the table above, or others (Strength, Haste, a short dash on
   double-jump, which needs the spike to check it's possible on stable).
2. **Charge:** are the powers free once crafted, or do they **draw down a charge** that a
   Power Pack refills (a meter on the actionbar, so powers fade when it's empty)? Free is
   simpler; charge keeps Power Packs worth farming.
3. **The template:** a vanilla netherite template, or the Power Pack as the template.
4. **Keep on death:** like the tricorder, or dropped like normal armor (the Power Pack
   charm (M10) already protects it)?

**Spike first:** a smithing-transform recipe with a custom result keeps enchantments and
damage on Bedrock; a custom wearable with an attachable shows on the Xbox; and the
effect refresh doesn't flicker.

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
| Admin (owner) | `mc_command` (any console command) and `mc_server_action` (every server action) — superseded the four planned admin tools, 2026-10-11 | Look-only commands run at once; everything else is staged as a `minecraft` Proposal the owner approves, never silent |
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

**P1 shipped (2026-10-10):**
- **The persona:** `minecraft_dave`, with its prompt `agent-minecraft-dave-v1`, in the
  agent picker, and launchable from the Minecraft screen.
- **Your gamertag:** a `minecraft_gamertag` setting (on the Minecraft screen) that new
  chats start from.
- **15 tools:**
  - `mc_player`
  - `mc_server_status`, `mc_players`, `mc_play_history`
  - `mc_world_info`, `mc_locate` (structure or biome, from the player's last trail
    point)
  - `mc_what_is_at` and `mc_nearby` (2026-10-11, after the owner watched Dave hunt
    biome by biome for "what biome is at 0,0"): one call for the biome and surface
    height at a spot (explored ground, else the satellite's survey), and one for the
    nearest structure of every kind around a spot (the server's own `locate`, swept
    over Bedrock's structure ids checked on 1.26).
  - **The whole server** (owner, 2026-10-11: "expose ALL raw server tools to Dave, so
    he has everything needed"; `agent/minecraftadmin.py`). Reads run at once:
    `mc_server_log`, `mc_worlds` (slots, or one slot's rules), `mc_backups`,
    `mc_server_config` (server.properties, the allowlist, versions), and `mc_command`
    for a console command that only looks. **Everything else is a Proposal** of the new
    `minecraft` kind (migration 0227) that the owner approves inline in the chat: any
    other console command (`mc_command`), and every server action (`mc_server_action`:
    start/stop/restart, backup, load/create/update/reset/restore a world, rules,
    pin/delete backups, the allowlist, server.properties, auto-update, the update).
    A staged action stores the exact sidecar call, built from input the owner API's own
    models validate, and the executor re-checks the route against an allowlist at
    enact; a server refusal holds the leaf instead of reporting it done. This replaces
    the earlier plan's four admin tools (`mc_backup_now`, `mc_set_time`,
    `mc_set_weather`, `mc_announce`): every one is now a case of these two.
  - `mc_goals`, `mc_goal_create`, `mc_goal_update`, `mc_log`, `mc_log_read`
  - `mc_memory_read`, `mc_memory_add`, `mc_memory_replace`, `mc_memory_remove`
- **Migration 0226:** `mc_goals`, `mc_goal_log`, `mc_player_memory`, `mc_chat_player`,
  owner-only, with isolation tests.
- **Moved to a follow-up PR:**
  - the player sheet's Goals list, which needs a small owner API over the goal tables;
  - the admin tools as Proposals — shipped 2026-10-11 as the whole-server reach above
    (`mc_command`, `mc_server_action` and four reads, migration 0227).
- **Local model:** there is no per-persona route. Minecraft_Dave runs where `agent.turn`
  does, which on the box is `local:qwen3.8-flash-next` (checked 2026-10-10).

**Waves:**
- **P1 — the persona with what works today.**
  - The persona: its prompt, the picker entry, and launch from the Minecraft screen,
    with **the chat bound to one player**.
  - Worldgen `locate` and world info.
  - Server status, players and play history.
  - **Goals, the progress log and player memory**: their tables (with RLS tests), the
    tools, and the Goals list in the player sheet.
  - Web search and fetch.
  - The admin tools as Proposals (shipped 2026-10-11: every server capability, see
    above).
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

**Pending:** the owner's gamertag, which is entered on the Minecraft screen. Until it's set,
new Minecraft_Dave chats have no player and ask whose chat it is.

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

**Deliberately not tools**: giving items, teleporting, building, or editing blocks. There
are two owner-approved, narrow exceptions (2026-10-10): Dave hands a player **their own
tricorder** (M9), and **teleports a player to a known place for one Power Pack**, after
they confirm on a form (M10). Beyond those, nothing is given or teleported, and there is
no cross-player teleport. Also not tools: reading
anything outside the active slot; anything that touches JBrain notes or the wiki; free-form
console access for players.

## 3c. Vanilla-client compatibility (owner requirement, 2026-10-10)

**Every player-facing feature must work on an unmodified, store-installed Bedrock client on
Windows and Xbox.** That rules out client mods, resource packs players install themselves,
and launcher tricks. A pack the server sends on join is allowed, because it's
automatic. The rules that keep it true:

1. **Everything runs on the server.** The behavior pack's script runs inside BDS. The client
   only ever gets standard protocol messages.
2. **A resource pack only if the server pushes it on join** (owner decision, 2026-10-10).
   - **Allowed:** a small `jbrain` resource pack that BDS sends to every client as it
     joins. It downloads automatically, with nothing to install, no third-party tool or
     site, and it works the same from a computer or an Xbox.
   - **How:** the wrapper installs it with the behavior pack into each world it loads
     (`world_resource_packs.json`) and pins `texturepacks-required=true`. A client that
     declines the pack can't join, rather than joining half-working.
   - **What it carries:** the icons for the tricorder (M9) and the Power Pack (M10), and
     any later custom model, such as an NPC (M7).
   - **Kept small:** a few kilobytes of icons, so the join stays quick on the Xbox.
   - **Still ruled out:** anything a player would have to install themselves.
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
| Power Pack (M10) | a custom item and crafting recipe; its icon arrives in the automatic join download | ✅ by decision, confirmed on the Xbox and Windows in M0b |
| Power Pack teleport (M10) | a server form with Yes and No, then a normal teleport with vanilla sound and particles | ✅ |
| Power Pack charm (M10) | ordinary death and drops; the eye vanishes; a chat line on respawn | ✅ |
| Tricorder (M9) | a custom item (icon from the join download) with an actionbar arrow while held | ✅ by decision, confirmed in M0b |
| The `jbrain` resource pack | downloaded automatically on joining; nothing to install | ✅ owner-approved, 2026-10-10 |
| Waypoint markers in the world (optional) | vanilla particles at a spot, visible only to the asker | ✅ |
| Reminders and warnings | chat or actionbar, plus a vanilla sound | ✅ |
| Maps | a short code typed into a browser; nothing is shown in-game | ✅ no in-game image |
| `my_inventory` / `my_stats` / deaths | read on the server by the script | ✅ |
| Admin tools (time, weather, backup) | ordinary server commands | ✅ |
| Companion NPC (M7) | a vanilla mob, or a custom model sent in the join download | ✅ |
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
2. **Per-player keep inventory: DECIDED → the Power Pack charm (M10, owner,
   2026-10-10).** A player keeps their inventory on death if they carry a Power Pack
   (crafted from nine Eyes of Ender), and one pack is used up. The tricorder is always kept. The approach below is the one
   M10 uses. Bedrock's `keepInventory`
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
public or unallowlisted servers. Letting the bot build, teleport, or give items, beyond M9's tricorder and M10's Power-Pack-paid
teleport to a known place.

## 7. Terminal-dependency gaps (non-negotiable #10)

- **Publishing UDP 19132 on the LAN** comes for free with the compose `ports:` entry once the
  profile is created by an in-PWA update. No host step is needed.
- **Internet play (R1)** is switched on from the PWA. The UPnP mapping, the DNS record, the
  relay claim link and the outside test are all on the Ops card. The only possible non-box
  step is turning on UPnP or adding a manual forward in the router's own app, if the router
  has UPnP off. No step touches a box terminal.
- **Nothing else** in M1–M6 needs host access. If M0 finds a step that does, it gets designed
  out before the wave is scheduled.
