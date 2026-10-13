# Minecraft Bedrock — an on-box world server, its backups, and a companion that knows the world

> **Status:** In progress · **Last verified:** 2026-10-13 · **Waves:** M0✅ M1✅ T1✅ M2✅ M3✅ M4✅ M5✅ M6✅ M7◻️ M8✅ M8a✅ M9✅ M10✅ M11⏸️ M12✅ M13⏸️ M14⏸️ M15✅ M16✅ M17✅ M18✅ M19✅ M20✅ M21✅ M22✅ M23✅ M24✅ M25✅ M26✅ R1◻️ P1✅ P2✅ P3✅ SW1✅ SW2✅ SW3✅ SW4✅ DW1✅

**What's left (audited against the code, 2026-10-13).** M0, M5, M6, M8, M9 and P2 are built;
each section's "Remaining" paragraph names its small leftovers. Genuinely open, each the
owner's call:
- **R1:** the `mc.hopkinsbrain.com` name and IP updater (put off), a UPnP helper, an
  Internet-play switch with a Test button; a brother joining from his own house is unrecorded.
- **M4:** built (§M4e, 2026-10-13: villagers and trades, pets, named mobs, the Nether
  pointer). Left: pets and villagers on the map, chest minecarts in `mc_find_item`, and
  a fast-path answer for "where's my dog" (§M4e "Not built").
- **M7:** a companion NPC, scheduled backups, the other §3b tools.
- **M8:** beds and saved places on the map, height / slice / changes layers, Dave's in-game
  "map of here" link.
- **M5/M6/P2:** the companion's name as a setting, per-player rate limits, an in-game web
  switch, a latency measurement, and `auto` log entries for crafting.
- **P1 follow-up:** done: goals in the PWA (§M24, "Goals in the app") and §M25's scan on
  the map are both built.
- **Box checks:** `docs/runbooks/MINECRAFT_HOME_TEST.md`.

The owner wants a Minecraft **Bedrock** dedicated server on the box. They need to start and
stop it, back up its world, and **import an existing world** they already play. On top of
that, they want a **chat companion** that players talk to from inside the game and that
answers from the world's real data: "where's the nearest pig?", "where's the nearest woodland
mansion?", "where did I die?".

**The story is not kept here.** The world's lore is canon in the Bleepbloop_City repo's
[`notes/lore.md`](https://github.com/jeffmhopkins/Bleepbloop_City/blob/main/notes/lore.md):
the Old Makers, Dave the satellite AI, the crew, the Tricorder, death as the transporter,
and the words Dave says. That page is the **source of truth** (owner, 2026-10-11). This
plan only says how jbrain2 implements it. When they disagree, the lore page wins and this
plan and the code are fixed to match.

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
| `GET /world/index` | Parses the latest snapshot and streams JSONL records (M4). Not built: the map (§M8) and chest search (§M23) read the live world's LevelDB directly instead — BDS holds LevelDB's lock, but the stdlib reader never takes it, and table files are immutable while the log is only appended to. |
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
  - **The items have left the kit (2026-10-11):** the Tricorder, the Power Pack, the recipe,
    the icons and the arrow are in the always-installed `jbrain` packs now (M9/M10/DW1,
    below), and the kit only logs. The runbook's rows 10–16 check them.
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

**Done by hand, 2026-10-11 (owner, over the debug token).** The router app (Spectrum,
SAX2V1R) showed a public WAN IPv4, so there is no CGNAT. The owner reserved 192.168.1.10
for the box and added the forwards there: TCP+UDP 19132, and UDP 19140–19144. Over the
debug API, `server-udp-ports` was set to `<public-ip>:19140-19144:19140-19144`, and the
server restarted with nobody on. Results:
- From home, `192.168.1.10:19132` joined. The public address from inside the house
  didn't, as expected when the router doesn't loop back.
- A phone on mobile data joined at the public IP, port 19132.
- The allowlist is on.
- The PWA's Join card now shows the public IP beside the LAN one, and warns when it no
  longer matches the announced one.

Still open: the DNS name and the updater (the owner put Cloudflare off), the UPnP helper,
and the Test button. Until the updater lands, a changed home IP means a debug-API
`server-udp-ports` update, which the Join card's warning prompts.

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

> **Status (2026-10-12):** the tables below were never needed for what has been built.
> Explored ground (§M8), blocks (`mc_find_block`) and chest contents (§M23,
> `mc_find_item`) are read straight off the live world's LevelDB by in-memory indexes in
> the sidecar, refreshed when the files change — no snapshot parse, no `app.mc_*` world
> tables. Entities (villagers, pets) were what was left of this wave; §M4e built them the
> same way on 2026-10-13, so M4 is done in that shape.

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

#### M4e — Dave knows villagers and pets: the entity index (designed and built, 2026-10-13)

> **Status:** built 2026-10-13 (waves E1 and E2, one pass), with the owner's five
> decisions of 2026-10-13 below. It was what the Status note above called "what's left of
> this wave", and it carries §M9's Nether pointer. "What was built" at the end of this
> section is the as-built record; where the design text above it differs, the decisions
> and the as-built record win.

**Why.** "Who sells Mending?", "where's my dog?", "where did Clover go?" Villagers, pets and
named mobs live in the world's files the way chests do, and Dave can't see any of them
today: `mc_find_mob` (§M22) asks the game for the nearest mob of a type, and only within
the chunks loaded around online players.

**What the files hold (checked 2026-10-13 against BDS 1.26.52.3, on a copy of a dev world,
with our own `leveldb.py`/`nbt.py`).**
- **The keys.** Every entity is its own record, `actorprefix` + 8 bytes, holding one NBT
  compound. Each chunk has a `digp` record listing the 8-byte ids of the entities in it.
  The `digp` key is `digp` + x + z (12 bytes, the Overworld) or + x + z + dimension (16
  bytes). The entity's own NBT carries no dimension, so **the dimension comes from the
  `digp` key**. In all five test worlds every actor was listed by a `digp`, and none had
  the pre-1.18.30 per-chunk `Entity` record (key tag 50). An imported old world can still
  hold tag-50 records in chunks nobody has loaded since 1.18.30. They are back-to-back
  compounds, the same shape as the tag-49 block entities `containers.py` already reads.
- **The fields** (all read cleanly by `nbt.py`; no reader change needed):
  - **What it is:** `identifier` (`minecraft:villager_v2`, `minecraft:wolf`, …). **Where:**
    `Pos` (3 floats).
  - **Identity:** `UniqueID` (int64). The script API's `entity.id` is the same number as a
    string: the probe printed `id=-8589934591` for the wolf whose file says `UniqueID:
    -8589934591`.
  - **Name:** `CustomName`, plus `CustomNameVisible`.
  - **Pets:** `IsTamed`, `OwnerNew` (int64, −1 when nobody owns it), `Sitting`, `IsBaby`.
    `definitions` names the coat in words (`+minecraft:cat_calico`,
    `+minecraft:base_gray`), which is better than the bare `Variant`/`MarkVariant`
    numbers.
  - **Villagers:**
    - `PreferredProfession` (`"librarian"`, `"cleric"`, …) is present once a villager has
      a job. `Variant` is the same as a number (0 unskilled, 6 cartographer, 7 cleric, 8
      armorer, 13 mason, 14 nitwit), and `definitions` carries `+librarian`.
    - `TradeTier` is the level, 0–4 (Novice → Master). `TradeExperience` > 0 means it has
      been traded with, which locks in its job.
    - `Offers.Recipes[]` lists each trade: `buyA`/`buyB` and `sell` item stacks, `tier`,
      `uses`/`maxUses`. An enchanted book's enchantment is under `sell.tag.ench[]` as
      `{id, lvl}` with Bedrock's numeric ids. The experiment's librarians sold ids 34, 30,
      4, 25, 22, 20 and 14; Mending is 26. So we need a small static id → name table.
  - **Trades exist without a player:** a villager summoned with a job had its full `Offers`
    for every tier, written by the server before anyone opened its trade screen. A
    villager's job can still change until it's traded with. The "librarian" summoned in
    the experiment took a free cartography table and saved as a cartographer.
- **Owners.** `OwnerNew` holds the owner's `UniqueID`, which is the owner's script
  `player.id`. The pack already keys each player's records by that id (`jb:known:<id>`,
  `jb:tricorder:<id>`). The player records in the files (`player_server_<uuid>`) carry no
  gamertag, so the gamertag ↔ id link has to come from the pack. Not checkable here (it
  needs a real client to tame something), so it's a box check.
- **Portals.** One `portals` key holds `data.PortalRecords[]`: `DimId`, `TpX`/`TpY`/`TpZ`,
  `Span`, `Xa`/`Za`. A portal lit with fire through the console, with no player, was
  recorded at once. `leveldb.read_keys` reads that one key without a full sweep, so it
  costs almost nothing.

**What the live game can see** (stable `@minecraft/server` 2.0.0, the same probe):
- `getEntities` and `nameTag` work, and so do `minecraft:variant` (a villager's job number)
  and `minecraft:mark_variant`.
- **A tamed pet has no `minecraft:tameable` component.** The tamed wolf, cat, parrot and
  horse showed only `minecraft:is_tamed`, which has no owner field. So
  `tamedToPlayerId` can't be read for the pets that matter.
- No villager component exposes trades or level.
- **So the split is:** the index answers everything here (trades, owners, unloaded
  chunks). It is made current the way §M23 does it: `flush=1` runs the `save hold` dance,
  which also writes out the entities loaded around players. The pack's only change is to
  send the asker's `player.id`. `mc_find_mob` stays as it is, for "nearest cow".

**Design.**
- **Sidecar `entities.py`: `EntityIndex`**, modelled on `ContainerIndex`.
  - **Refresh:** the same 60 s re-read when the db files change.
  - **One `leveldb.entries` sweep** keeps `actorprefix`, `digp`, and legacy tag 50.
    Newest sequence wins.
  - **Only candidates are parsed:** a villager or wandering trader, anything with
    `CustomName`, or `IsTamed` set. This is a byte test on the raw value before any parse
    (`b"CustomName"`, the identifier, `b"\x01\x07\x00IsTamed\x01"`). Every other value is
    dropped as it streams past, so a world full of cows and bees isn't held in memory.
  - **Placement:** only actors listed by a `digp` are indexed, with the dimension and the
    chunk taken from it.
  - **What's kept per entity:** kind, x/y/z, dimension, name, owner id, sitting, baby, coat
    words. A villager adds its job, level, locked-in flag, and at most 32 trades as
    `(tier, buy, buy2, sell, enchantments, uses, max_uses)`.
  - **Caps:** at most 20,000 villagers, 5,000 pets and 5,000 named mobs, each overflow
    counted. Unreadable records are counted, as in §M23.
  - **Cost:** measured here at 0.08 s for the sweep of a 5.5 MB db and about 290 µs to
    parse one actor. Parsing only the candidates cuts the parsing by about 4× in a village
    world. On a big world the decompression dominates, as it does for the container index.
    So the two indexes **share one sweep** (a combined key filter, dispatching on the key)
    rather than each decompressing every table.
- **Sidecar routes:**
  - `GET /map/villagers?slot&dim&x&z&profession&sells&enchant&radius&limit&flush`
  - `GET /map/pets?slot&dim&x&z&owner&kind&name&radius&limit&flush`. `owner` is a
    player id. `name` matches any named mob, casefolded.
  - `GET /map/portals?slot&dim`
  - Every answer carries `as_of`/`age_s`, the index counts and `flush`, as `/map/items`
    does. A debug passthrough is added for each, for the box checks.
- **The asker's id.** The pack adds `pid` (`player.id`) to the `dave` event.
  `bds._pack_event` accepts it only as a decimal string. The sidecar remembers gamertag ↔
  id per world, so the app's "Hank's pets" works while Hank is offline.
- **Backend tools** (both Dave tiers, panel labels, prompt bump):
  - **`mc_find_villager(profession?, sells?, enchantment?, x?, z?, radius?, limit?,
    dimension?)`.** Each hit gives the job, level ("an Expert librarian"), whether its job
    is locked in, and the matching trade and its price ("Mending I for 12 emeralds and a
    book"). It says when a trade is used up until the villager restocks.
  - **`mc_find_pet(kind?, name?, gamertag?)`.** In game, `kind` lists the asker's own pets
    (and, by decision 1, any player's by `gamertag`, or everyone's with `anyone`).
    `name` finds a named mob, the asker's own pets first. Player-written names are fenced,
    as container names are. It aims the Tricorder at the nearest hit, like every finder.
- **Cost (§M17).**
  - Villagers follow §M23 exactly: free within 48 blocks (the Tricorder's short-range
    scan), a deep scan farther out, a repeat free.
  - ~~Your own pets are free anywhere~~, ~~a named mob found by its exact name is free~~:
    **superseded by decision 3** (2026-10-13). Pets and named mobs follow the chest rule
    too, your own included.
- **Item guide (§M19):** no gating. Villagers and pets aren't Old Maker items, and no reply
  names one.
- **Nether pointer (§M9 leftover), same pass.** When `mc_set_waypoint` sets a target in
  another dimension, the record gets `via`: up to 8 known portals in the player's current
  dimension, from `/map/portals`. Held in the wrong dimension, the Tricorder points at the
  nearest of those ("Nether portal · 40 blocks"), and still names the target's dimension.
  A portal record can outlive a broken portal, so before a portal is sent, `BlockIndex`
  checks that `minecraft:portal` is still at its `Tp` spot (a box check).
- **Not built (later):**
  - Pets and villagers on the map (an overlay, as §M26's beacons).
  - Chest minecarts and chest boats in `mc_find_item` (they are actors, so this sweep
    makes them cheap; §M23's "Not built").
  - Allays, which aren't tamed in the files' sense.
  - The fast path for "where's my dog", until its line is signed off.
  - Live `jb:find` by name.
  - Villagers' gossip, and cure discounts (`*CuredDiscount`).
- **Lore.**
  - **Answers:** Dave answers from "what it has seen: the saved world". He says "as of N
    minutes ago" the way §M23 does, and never says what villagers are. Whether they're
    the Old Makers' descendants is an open question on the lore page, sealed for now.
  - **Two new lines, signed off on the lore page 2026-10-13** (decision 4; the second
    without its last sentence):
    - **Pet found:** **[Dave]** *Rex* is *120* blocks *north-east*, sitting right where
      you left him. Hold your Tricorder and follow the arrow.
    - **Trade found:** **[Dave]** From up here I see a *librarian* *230* blocks *west* who
      sells *Mending* for *12* emeralds.
- **Effort.** About 1.5 agent-days, in two waves:
  - **E1:** `entities.py`, the shared sweep, routes and debug, the two tools, the `pid`
    plumbing, and tests built from fixtures recorded from this experiment. About a day.
  - **E2:** portals, `via` on the waypoint, the pack arrow and its sim test. About half a
    day.

  No new tables: the index is in memory, as §M23's is.
- **Box checks (after deploy).**
  - That `OwnerNew` equals the taming player's `player.id`, including for a horse.
  - The counts and time of a cold read on the owner's world, in the Nether too.
  - That a broken portal leaves its record, and that the block check catches it.

**Owner decisions (2026-10-13, answered).** Where these differ from the design above, they
win:
1. **Whose pets: anyone's.** In game a player can find anyone's pets, not only their own,
   and any named mob. (Not the recommendation: others' pets aren't limited to a name.)
2. **Which trades: unlocked levels only.** Dave tells only the trades a villager offers
   now (tiers up to its `TradeTier`), never locked ones. (As recommended.)
3. **Cost: everything follows the chest rule, own pets included.** Free within 48 blocks
   of the asker, farther costs one deep scan (§M17), a repeat by §M23's rule free; the
   app is unlimited. (Not the recommendation: own pets and named mobs aren't free at any
   distance.)
4. **The lines** are canon on the lore page ("Villagers and pets"): the pet line as
   proposed, and the trade line without its last sentence ("The villagers still trade in
   emeralds without knowing why" was dropped).
5. **The Nether pointer ships in the same pass** (E2). (As recommended.)

**What was built (2026-10-13).**
- **Sidecar** (`deploy/minecraft/entities.py`): `EntityIndex`, as designed. One shared
  sweep: `containers.refresh_together` re-reads every index of a world whose files
  changed in ONE `leveldb.entries` pass, each index taking its own keys (`wants`) and
  dropping a value it won't use as it streams past (`keeps`, the byte test), so the chest
  and entity indexes decompress the tables once. Placement and the dimension come from
  `digp` (an actor no `digp` lists is counted as `unplaced`); legacy tag-50 records are
  read by their own key. Trades above the villager's level are dropped at parse and only
  counted (`locked_trades`), so no route can tell one. The enchantment id → name table is
  Bedrock's 0–40 (Mending 26). Pet kinds take everyday words (`dog` → `wolf`).
- **Routes:** `/map/villagers` (`profession`, `sells`, `enchant`), `/map/pets` (`owner`
  id, or `owner_name` resolved through the world's learned gamertag ↔ id table,
  `owner_known: false` when it hasn't learned that player yet; `kind`, `name`,
  `everywhere`), `/map/portals` (`check` drops a record whose `minecraft:portal` block is
  gone, by `BlockIndex` within 4 blocks of its `Tp` spot). Each carries `as_of`/`age_s`,
  the index's counts and `flush`, as `/map/items`. The learned ids live in
  `player-ids.json` in the sidecar's data dir, written when a `/dave` carries `pid`.
- **The asker's id:** the pack's `dave` event carries `pid` (`player.id`); `bds` keeps it
  only as a decimal string and the door puts it on the `Asker`.
- **Tools** (both Dave tiers; prompt `agent-minecraft-dave-v22`; `mc_find_villager` v1,
  `mc_find_pet` v1, `mc_set_waypoint` v5):
  - `mc_find_villager(profession?, sells?, enchantment?, x?, z?, radius?, limit?,
    dimension?)`: each hit is "an Expert librarian at x= y= z=, 230 blocks west: sells an
    enchanted book (Mending I) for 12 emerald and 1 book", says when a trade is used up
    until it restocks and when the job is locked in, and that only today's trades are
    told. Player-given names are fenced.
  - `mc_find_pet(kind?, name?, gamertag?, anyone?, dimension?, x?, z?, limit?)`: in game,
    no gamertag is the asker's own pets (by their `pid`; by gamertag from an older pack);
    `gamertag` is any player's, `anyone` every tamed pet, `name` any mob so named, the
    asker's first. Pets in other dimensions are listed after, with no distance. Names
    and owners are fenced.
  - **Cost, in game:** both look within 48 blocks of the asker first, free (the
    Tricorder's short-range scan). If that finds nothing and the ask reaches farther (no
    radius, a wider one, or a centre elsewhere), the search is a deep scan through
    `scans.deep_scan`: a repeat of the same search from within 256 blocks is free, and a
    search that matched nothing anywhere is given back (as §M23's unknown item id). An
    owner Dave hasn't learned yet is said, and costs nothing.
  - **Wording:** each answer asks Dave to point the Tricorder with `mc_set_waypoint` and
    to answer in the canon's lines: "*Rex* is *120* blocks *north-east*, sitting right
    where you left him. Hold your Tricorder and follow the arrow." (the middle only for a
    sitting pet) and "From up here I see a *librarian* *230* blocks *west* who sells
    *Mending* for *12* emeralds."
- **The Nether pointer (E2):** `mc_set_waypoint` adds `via` to a target in the Overworld
  or the Nether: up to 8 standing portals on the other side, nearest first to the asker
  when they're there, else to the target mapped across (÷8 / ×8). Pack 1.12.0:
  `parseWaypoint` keeps at most 16 well-formed `via` entries, and the held Tricorder in
  the wrong dimension reads `↑ Nether portal · 20 blocks · Base is in the Overworld`
  (`You're here` within 3 blocks); with none known, the old line. The pack's reply adds
  `via N portal(s)`.
- **Tests:** the sidecar's against byte-level records recorded from the experiment world
  (`supervisor/tests/fixtures/mc_entities.json.gz`: twelve actor records, their `digp`
  entries and the `portals` record), with owner, level, uses and Mending patched into
  those bytes in place; the tools' in-game scoping, cost and wording through the real
  handlers; the pack's `pid` and `via` arrow in the node sim. No new tables.
- **Debug:** `GET /minecraft/map/villagers`, `/map/pets`, `/map/portals`
  (`docs/runbooks/DEBUG_ACCESS.md`).

**Box checks (after deploy).** Those above, and: that `OwnerNew` equals the taming
player's `player.id` on a real tame (a wolf and a horse), seen as `/map/pets?owner_name=`
listing it after they ask Dave anything; a villager's trades in its trade screen against
`/map/villagers` (the unlocked ones only); the Tricorder's portal arrow in the Nether on
a real client; and the cold-read time with both indexes sharing the sweep on the owner's
world.

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
  - **Players type `/dave`, `/d` or `/ai`** (owner, 2026-10-12). Bedrock answers every custom
    command without its namespace too (box check: `dave hi` reaches the handler, an
    unknown `zzdave` doesn't), so the pack registers `jb:dave`, `jb:d` and `jb:ai` on one handler,
    and everything players read names `/dave`. The namespaced forms keep working.
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

**The in-game door, first cut (built 2026-10-11, owner: "Dave, point me home" / "Dave,
mark this location as tree farm" / "Dave, navigate to tree farm" — "this is what we
should be able to do once we're live").** Not yet tried from a real client.
- **The command** lives in the always-installed `jbrain` pack (`deploy/minecraft/jbrain-pack`),
  not the probe pack, so every world has it and the two packs can't register one name
  twice. `/jb:dave <words>` logs one `[jbrain] {"ev":"dave",…}` line with the asker's
  name, words, block position and dimension, and their bed or respawn anchor
  (`player.getSpawnPoint()`, `null` when unset), then tells the asker "Dave is
  thinking…". Each word is its own `String` parameter, so `point me home` needs no
  quotes. **Checked on BDS 1.26.52:** the engine caps a custom command at 8 parameters
  (24 refused the whole command), and a `String` parameter takes one plain word or one
  `"quoted phrase"` — a comma or a bare number is a syntax error the player sees. So
  "Dave, …" (the comma) and coordinates go in quotes: `/jb:dave "Dave, mark this
  location as tree farm"`; a leading "Dave," is dropped either way. A console origin is
  refused ("Only a player can ask Dave").
- **The sidecar** (`bds.py`) turns the line into a `dave` event carrying the **xuid BDS
  logged when that name connected** — the script API has no xuid, and the line's own
  fields are never trusted for identity. A name that never connected, or an empty
  question, is dropped. Because the line quotes the player's raw text, a `[Scripting]`
  / `[jbrain]` line is only ever read as the pack's, and BDS's own lines (`Player
  connected/disconnected`, `Server started.`, `Version`, the `save query` ready line)
  count only right after BDS's `[… INFO] ` prefix, so a question can't forge a join
  under someone else's xuid (security N3).
- **The api** (`jbrain.minecraft.door`, started in `main.py` beside the session and
  track drains) polls `/events` and runs each fresh question (≤ 2 minutes old, so a
  restart never answers an old one late) as one headless Minecraft_Dave turn on the
  scheduled-task path (`LoopTurnExecutor`, on the workshop slot so it never evicts the
  owner's chat prefix), in the asker's own continuing session
  (`mc_chat_player.door`, migration 0228; a new one after 6 hours idle, the old one kept
  in the PWA). Player tier (`agents.narrow_for_ingame`): no web, no `mc_command` /
  `mc_server_action`, no log/config/world/backup reads, no `mc_player`. While the turn
  runs, the tools see the asker (`minecraft/asker.py`): their position and dimension are
  the default for searches, places and waypoints, and any other gamertag is refused.
  The question is fenced; the asker's position and bed come in a plain line before it.
- **The reply** is `tellraw "<gamertag>" {"rawtext":[{"text":"<Dave> …"}]}` through the
  console, built with `json.dumps` (no reply can end the command), stripped of
  markdown and `§` codes, in chunks of ≤ 220 characters and ≤ 900 in all. One turn at a
  time per player: up to 3 more wait ("One moment…"), beyond that "Hang on…"; at most two
  players' turns run at once. A failed turn answers a short apology and is logged.
- **Not yet:** the companion's name as a setting (the command is fixed at `jb:dave`),
  per-player rate limits beyond the queue, live `scriptevent jb:q` queries, and the
  owner setting to allow the web in game.

**Remaining (checked against the code, 2026-10-12):** the door itself is built
(`/dave`, `/d`, `/ai`; the `jbrain` pack, `bds.py`'s `dave` event,
`jbrain.minecraft.door`), and live lookups ride their own scriptevents (`jb:find` for
§M22's mobs, `jb:waypoint`, `jb:transport`). Left: the companion's name as a setting,
per-player rate limits beyond the queue (§M17 limits only deep scans), and the owner
setting to allow the web in game. A general `jb:q` query channel is not needed while
each live lookup has its own event.

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

**Remaining (checked against the code, 2026-10-12):** the persona is built
(Minecraft_Dave, P1) with the in-game player tier (`agents.narrow_for_ingame`: no notes,
wiki or web, scoped to the asker) and the live and worldgen ★ tools (`mc_find_mob`,
`mc_locate`, `mc_players`, `mc_nearby`, `mc_world_info`, the asker's own position and
bed). Left: the name as an Ops setting (it is fixed as Dave), the index rung of the
cascade and `find_container` (both wait on M4's index), per-player rate limits, and the
measured reply latency against the 5 s target.

### M7 — Nice-to-haves (each is its own small decision)

- The non-★ tools in §3b, one per PR, in whatever order players actually ask for them.
- **A Dave quick menu**: `/dave` with no text opens a server form (`@minecraft/server-ui`
  `ActionFormData`). The form has buttons for *Where am I*, *Nearest…*, *Guide me home*,
  *My last death*, *Map of here* and *Save this spot*. It is vanilla-safe (§3c), and it is
  optional since everyone has a keyboard.
  **Built (owner-approved 2026-10-13, pack 1.11.0):** `/dave`, `/d` or `/ai` with nothing
  after it opens an `ActionFormData` titled "Dave" with *Where am I?*, *Point me home*, *My
  last death*, *Save this spot* and *Ask something else…*. Each button asks its own words
  through the same path as typing them after `/dave` (the Tricorder check, the unlocks, the
  fast path §M18, then the model), so it adds no Dave lines. *Save this spot* asks for a
  name in a `ModalFormData` box ("Name this spot") and sends "save this spot as <name>";
  *Ask something else…* sends what's typed in an "Ask Dave" box; closing either does
  nothing. Without their own Tricorder the player gets the usual static and no menu. A
  command runs with the chat still open, where a form can't show (`UserBusy`), so the
  menu is asked again each second, as the transport offer is, and after 10 s the old
  "Ask Dave something…" hint goes to chat instead. The fast path answers *Where am I?*,
  *Point me home* and a plain *save this spot as <name>* (a name of up to four plain words
  without "my", "the" and the like); *My last death* goes to the model, as does a name the
  fast path won't take. *Nearest…* and *Map of here* are left out: the first needs a
  second menu of things, the second waits on M8's in-game link. Tested in
  `jbrain-sim/pack.test.mjs`. Box check: home test row 22.
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

**Shipped 2026-10-11** as the map viewer's wave 2 — what was built, and the gaps it left, are
under §M8, *Viewer, wave 2*. The session stepper below was replaced at the GUI gate by the
Days navigation (`docs/mocks/minecraft-map/README.md`, *Session navigation*).

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

**The viewer's design is settled (owner, 2026-10-11): `docs/mocks/minecraft-map/` variant
A** — the map full-bleed under one persistent bottom panel (world, status, Layers /
Timeline), explored ground in full colour over the dimmed satellite survey in all three
dimensions, zoom from 1 px per chunk to 8 px per block, a status line that never says
Live while replaying, clustered structure markers, tap-a-spot (biome, y, explored or
survey, Copy X Z), a long-press pin, and the same component as the inline card in
Minecraft_Dave's chat. **Session navigation is 3 · Days**: a day strip with a dot per
player who played, that day's sessions as chips, and each player's full session list
behind **Sessions**. The mock README lists the four DESIGN exceptions approved with the
pick and the build gaps it named (a saved structure sweep; the travel-log session API),
both since built: waves 1 and 2 below.

**Viewer, wave 1 — the live map (shipped 2026-10-11).** A's live view, without the
timeline (DESIGN.md "The Minecraft map"):
- **The screen** (`frontend/src/screens/MinecraftMap.tsx`, its geometry and words pure in
  `frontend/src/minecraftMap.ts`) is a page pushed from the Minecraft screen's **Map** row or a
  world's page: the full-bleed tile map (zoom 0–4 from the box, then nearest-neighbour up to
  8 px per block), pan, pinch, wheel, + / −, show everyone, the dimension switch, the scale
  bar, and the persistent panel with the world picker, the honest status line and Layers.
  Tiles are re-fetched after a minute while the world is live. `dev:mock` paints the
  reviewed fixture's world procedurally (`frontend/src/minecraftMapFixtures.ts`).
- **Tap a spot** → `GET /api/minecraft/map/point?x&z&slot&dim` (the owner twin of the debug
  route, same bounds): biome, y (`~` for the survey), source, **Copy X Z**. **Long-press** pins
  a block, with a one-block nudge.
- **Players** → `GET /api/minecraft/map/players?slot`: each player's newest
  `mc_player_track` sample in that world folder (`travel.latest_positions`), flagged online
  from the sidecar's `/status` when that world is loaded and running.
- **Structures, the saved sweep** (the build gap the GUI gate named). The sidecar's
  `deploy/minecraft/structure_sweep.py` plans a grid of points 512 blocks apart over the explored
  extent of each dimension (spawn first) plus 1,024 blocks of margin, capped at 4,096 blocks
  from its centre, asks `locate structure` for every kind from each, keeps each answer once
  and **saves it per world folder** as `/data/structures/<folder>.json` in the sidecar's
  volume. A JSON file there rather than a backend table: the sweep is the world's own derived
  data, read by the same process that makes it and the tiles, it must answer while the api's
  view of the world is a stopped server, and it holds nothing about people, so it needs no
  RLS; keeping it outside the world folder keeps it out of `.mcworld` exports. Structures are
  fixed by the seed, so a saved sweep goes stale only when the seed changes, when the explored
  extent grows more than 256 blocks past what was swept, or when a server update adds a
  structure kind; a view of a stale sweep of the loaded, running world starts a fresh one in
  the background (a failed one is retried after 15 minutes), and **Search again** /
  `POST /api/minecraft/map/structures/sweep?slot` starts one on demand. Read:
  `GET /api/minecraft/map/structures?slot&dim` (structures, `swept_at`, `stale`, `sweep`
  progress); debug twins of both.

**Viewer, wave 2 — the timeline, M8a (shipped 2026-10-11).** The Timeline tab of variant A with
session navigation *3 · Days* (DESIGN.md "The Minecraft map"):
- **API:** `GET /api/minecraft/timeline?slot&from&to&players&include&dim` (owner-only, the
  slot's world folder resolved like `/map/players`; query logic in
  `backend/src/jbrain/minecraft/timeline.py`). Over one range of at most 92 days it returns:
  - **trails**: each player's samples cut into *segments* at every session, dimension,
    death and respawn, then down to at most 2,000 points a player, keeping each segment's
    first and last point so nothing is ever drawn or interpolated across a portal, a log-off
    or a respawn (`downsampled` says when gaps no longer mean "standing still"). A life's
    segment ends on its death's own place (the pack's position), not the last sample before
    it: sampled every 10 s, a player who respawns at once leaves no sample where they fell,
    and the trail stopped short of the skull (fixed 2026-10-13);
  - **events**: joins and leaves (placed at the player's first and last sample, since the
    console gives them no position), deaths with the behavior pack's `cause` and `killer`,
    respawns, and dimension changes derived from the trail (a respawn's jump is not one);
  - **sessions**: every session of the world, newest first (at most 500), each with its
    start, its end or null while on, deaths, blocks walked per dimension (never the
    respawn's jump), the dimensions it visited and where it began. A session belongs to the
    world its join event was recorded in, so sessions from before the travel log (T1) aren't
    listed;
  - **explored** (asked for with `include=explored`): the fog of war in one dimension, each
    chunk's first-seen time up to the range's end, for everyone or the `players` given, at
    most 60,000 chunks.

  Raw reads are bounded too (300,000 samples, 5,000 events a range). The debug twin
  `GET /api/debug/minecraft/timeline?slot&from&to` gives the same range as counts only —
  samples read, points sent, segments, events by kind, sessions — the `/travel` rule: no
  trail leaves over a token.
- **The screen:** the Timeline tab opens pinned to now (still the live view); the day strip
  (the last week since the first session, a dot per player who played), that day's sessions
  as chips, the transport (play/pause, 1× / 10× / 60× / 600×, the clock, Follow, Now/Live),
  the scrubber with session bands and merged event glyphs, and the presets *Session · Today ·
  Week · All* and *Who's shown* one drag further. A day sets the range to its activity, a
  chip loads its session with 2 minutes either side and centres the map on its start, and
  the shared Sheet *Sessions* lists every session (a player's **Sessions** opens it filtered).
  Playback is a local redraw: dots interpolated between samples with a 5-minute tail, Follow
  switches dimension with a toast, deaths pinned with their cause and **Replay the minutes
  before**, and the status line reads *Not live · yesterday 4:48 pm — replaying at 600× ·
  positions from the travel log · terrain as of now*, with **Back to live**.
- **Overlays made real:** Trails, Deaths, Beds & spawn (the **world spawn**, which the
  sidecar's `/map/info` now reads from `level.dat`) and *Explored by* (Anyone or one player,
  lifting by first-seen time as the timeline plays).
- **Not yet (the box doesn't record it):** **beds**. The behavior pack reads a player's spawn
  point only when they ask Dave something (`/jb:dave`), and nothing keeps it, so the overlay
  draws the world spawn and says beds aren't recorded. A pack event on a bed or respawn
  anchor being set, kept per player, would close it. **Saved places** (`mc_places`) are kept
  but not drawn yet; the overlay stays *later*.
- **Next:** beds, saved places on the map, and the player share link.

**Viewer, wave 3 — the chat card, P3 (shipped 2026-10-11).** Minecraft_Dave's `minecraft_map`
tool-view (DESIGN.md "`minecraft_map` tool-view"), the GUI gate's *Chat card*:
- **Tool:** `mc_show_map(focus?, label?, x?, z?, dimension?, slot?, zoom?, show?)`
  (`backend/src/jbrain/agent/minecraftmap.py`), Minecraft_Dave's, PWA only. It checks and
  clamps every slot before the box is asked anything, resolves the loaded world, and fills
  `focus: death` from the chat player's newest death in that world's travel log (placed at
  the last sample before it when the pack gave no position), `player` from their last
  sample, `place` from their saved places. It returns the data-only view and a fenced text
  summary with the coordinates and, in the Nether, the Overworld side (×8). The view holds
  no URL: the card builds its own tile requests from the on-box route — the carve-out
  approved with the GUI gate, written into DESIGN.
- **Card:** `frontend/src/agent/views/minecraftMapCard.tsx`, drawn by the full map's own
  renderer, factored out into `frontend/src/screens/minecraftMapDraw.ts`. It has a 220-px
  map with the trail, deaths, structures, the focus and the scale bar, the honest status
  line, the focus with Copy X Z, overlay chips and a legend. One finger scrolls the chat and
  two pinch. A tap opens the full map on the card's spot: `MapLayer`'s new `at` (slot,
  dimension, centre, zoom, marked spot), reached through `minecraftMapNav.ts`.
- **Not done:** jerv's own map tool (§3b, "show me the map of the Minecraft world"). jerv
  reads the owner's notes, and the persona firewall keeps player-authored world text
  (gamertags, world and place names) away from a KB-reading agent, so it needs its own
  decision; the card itself is ready for it. The card's *Places* chip from the mock waits on
  saved places being drawn on the map.

**Renderer, first layer (shipped 2026-10-10).**
- **Reader:** `deploy/minecraft/leveldb.py` is a stdlib reader for Mojang's LevelDB
  (tables with raw-deflate blocks, plus the write-ahead log, newest write winning). It
  never writes or locks, so it reads a world while BDS runs.
- **Biome layer:** `mapping.py` renders the **biome atlas** from each chunk's `Data3D`
  record (heights and biomes, no block parsing), hillshaded, with ungenerated ground
  transparent (fog).
- **Surface layer (true colour, shipped 2026-10-11; owner: "the colours of the real blocks,
  instead of just biome").** Explored ground is drawn in its top block's colour: the
  height map says where to look, the chunk's `SubChunkPrefix` sections (the same palettes
  the block search reads) say what is there, and `blockcolors.py` colours it — rows for
  common blocks, rules for dye and wood families, glass and air looked through, water
  darkened by depth, an unknown block drawn in its biome colour. Each zoom works out only
  the columns it draws, cached per chunk until the next re-read, so a cold tile of a
  fully explored area costs about a second on the sidecar, not milliseconds; the tile cache
  hides it after that. The satellite survey under it stays biomes (the seed predicts no
  blocks), and the Nether stays biomes (its top block is the bedrock roof). The PWA picks
  **Surface** (default) or **Biomes** per device; tiles take `?layer=`. While a Surface
  tile is still being drawn, the PWA shows that spot's Biomes tile (owner, same day). The
  sidecar draws true colour outside its map lock, one at a time, so a Biomes tile is never
  queued behind one.
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
    between two biomes (re-measured 2026-10-11 over all 122,624 columns of the dev
    world's 479 chunks: 96.4%, 98% of misses within 4 blocks of a border);
  - heights are off by a median of about 2.5 blocks (90% of points within 9, trees
    included);
  - cubiomes speaks Java's ids; the 1.13+ oceans and every 1.16+ biome are translated
    (`JAVA_TO_BEDROCK`), each checked on a real spot;
  - the biome is read AT the predicted surface (the height map's own ids came from
    below it: a cave biome under a beach).

  **The Nether and the End are predicted too**, by Bedrock's own seeding rather than
  Java's. Bedrock seeds their noise with a Mersenne Twister on the seed's low 32 bits,
  not with `java.util.Random`, so cubiomes' Java answers were wrong there (3 of 4 Nether
  spots). `jbrain-predict` ports the seeding from Reed A. Cartwright's Bedrock fork of
  cubiomes (`reedacartwright/cubiomes`, branch `bedrock`, MIT; its LICENSE is
  byte-for-byte cubiomes' own). That seeding drives upstream cubiomes' noise, voronoi
  and End terrain code at the same pin, so there is no second dependency.
  - **Nether:** a 2D biome. The 4-block cell is read at y=0 from a double-perlin with
    Bedrock's amplitude, then the 1.14 voronoi seeded with the **full** 64-bit seed (the
    low 32 bits alone give 99.5%). Bedrock ids come out directly (8, 178–181), and the
    height is the roof (128), which is what a real Nether map shows.
  - **Nether accuracy:** **99.994%** of 349,184 sampled columns on seed
    7353120887189866808 (20 misses, all on borders), and 100% of 7,688 on a negative
    seed (−3913570224095895553). Of the 364,288 columns in the 1,423 Nether chunks saved
    in the local test worlds, all but 20 match.
  - **End:** biome 9 and the terrain's height, as the first air above it to match real
    chunks. Void is height **−1** (`PREDICT_VOID`): `Predictor.grid` leaves it out, so
    it draws transparent like ground nobody has generated.
  - **The End's one Bedrock constant:** the island noise is drawn after skipping
    **10,360** twister outputs, where the fork's Java-style 17,292 (66 perlins × 262)
    predicts no better than chance. The skip is 40 perlins (the terrain's 16+16+8
    octaves) × 259 draws (3 offsets + 256 shuffle swaps). It was found on 2026-10-11 by
    testing hypotheses against a real 1.26.52.3 world; no published source has it.
  - **End accuracy, land vs void:** **99.2%** of 10,217 points on the first seed and
    **98.7%** of 15,376 on the second, out to about 6,400 blocks. The misses are nearly
    all real land predicted as void: the small floating islets that the End-island
    *feature* places, which is decoration, not terrain noise.
  - **End accuracy, height:** 94–95% exact where both say land. On the 20 saved main
    island chunks, land vs void is 100%. Every height miss there is real ground ABOVE
    the prediction (the obsidian pillars), never below it.

  The Dockerfile's build-time smoke check now also asserts a warped forest in the
  Nether, plus an outer island's ground and the void just off it in the End. All are
  real spots on the first seed, and the Java-seeded binary fails all three. `map/info`
  says `predicted` in all three dimensions once the seed is known.

  **Overworld: upstream cubiomes stays.** Reed's fork generates the 1.16 *layered*
  Overworld, which 1.18 replaced with the noise Bedrock now shares with Java. Every
  column of the 479 real chunks of the dev world (122,624 columns, biome at the surface)
  was compared:

  | Overworld predictor | Zoom 0 (every column) | Zoom 2 (every 4th) |
  |---|---|---|
  | upstream cubiomes | **96.4%** | 96.4% (of 7,664) |
  | Reed's fork | 3.6% | 3.6% |

  For upstream, 4,304 of the 4,386 misses lie within 4 blocks of a real biome border.

  **Time per 256×256-pixel tile** was measured on a 4-core dev container, as the median
  of four tiles near and far from the origin. Zoom k samples every 2^k blocks.

  | Dimension | Zoom 0 | Zoom 1 | Zoom 2 | Zoom 3 | Zoom 4 |
  |---|---|---|---|---|---|
  | Overworld | 0.15 s | 0.66 s | 2.4 s | 2.3 s | 2.5 s |
  | Nether | 0.01 s | 0.27 s | 0.24 s | 0.27 s | 0.26 s |
  | End | 0.05 s | 0.15 s | 0.6 s | 2.3 s | 2.1 s |

  The End's cost is about 40 perlin samples per height cell of every 8-block terrain
  column. So its columns are split across the cores (single-threaded, zoom 3–4 took
  about 7 s), and the 625 island-noise reads per column come from one per-tile grid.
  A seed always predicts the same ground, so each tile is computed once and cached.

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
  - Owner-side jerv gets a `minecraft_map` tool that returns the image into chat. (Built
    instead as Minecraft_Dave's live `minecraft_map` card, viewer wave 3 above; jerv's own
    is still open.)
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
  **Built (2026-10-13, pack 1.11.0):** within the "You're here" 3 blocks, the holder alone
  hears `random.orb` (the XP pickup, at volume 0.6) once per arrival: it rings again for
  the same waypoint only after they've walked 8 blocks back out (`rules.js`
  `arrivalChime`). A player carrying their Tricorder without holding it hears it when the
  track drain's arrival clear (§M21) lands, unless the held arrow already rang for that
  waypoint. Not during a live scan (§M25): a scan's find isn't an arrival, and Dave's own
  clear (`end_scan`) never rings.
- **Target in another dimension:** `Woodland mansion is in the Overworld — take a portal`.
  The Nether pointer aims at the portal Dave knows from the M4 index (built in §M4e:
  `↑ Nether portal · 20 blocks · Base is in the Overworld`).
- **No target yet:** `Ask Dave where to go: /jb:dave …`.
- Putting the item away clears the actionbar. Nothing shows for anyone else.

**Waypoints, first cut (shipped 2026-10-11, owner: "a tool to set the tricorder waypoint
destination per user").** Dave's `mc_set_waypoint(x, z, y?, dimension?, label?,
gamertag?, clear?)` sends `scriptevent jb:waypoint {json}` over the console (the JSON is
`json.dumps`-escaped, so a label can't end the command). The `jbrain` pack's script (the
home-test kit's until 2026-10-11) stores it as a **world** dynamic property keyed by gamertag — saved with the world and
settable while the player is offline — and the held tricorder shows
`↗ <label> · 412 blocks · y -58` (the y when given), `You're here` within 3 blocks,
`<label> is in the Nether` from another dimension, and world spawn when none is set.
The script answers with a `[jbrain] waypoint set/cleared/refused` line (`[jbrain-probe]`
while it was the kit's), which the tool reads back: no answer means the pack didn't load
in that world, and the tool says so
rather than claiming it worked. Checked on a local 1.26 server: set (a gamertag with a
space), a refused bad dimension, cleared. **Saved places (2026-10-11):** `mc_set_waypoint`
also takes `place`, a name the player saved with `mc_place_save` (`app.mc_places`,
migration 0228: per player and world folder, names unique ignoring case, saving again
moves it; `mc_places` lists them with the way to each from the asker in game,
`mc_place_remove` forgets one). A place is saved where the player is right now, never at
typed coordinates (owner, 2026-10-11): where the in-game asker stands, or from the PWA at
their position while they're online (a sample under a minute old). Otherwise nothing is
saved. **"home"** is their saved home,
else the bed the in-game event reported, else Dave says neither exists and offers to mark
it. In game the answer also says which way and how far the target is.

**Shipped (wave mc-items, 2026-10-11): the Tricorder in every world.** `jbrain:tricorder`
and its set-A icon (`../mocks/minecraft-items/`) moved from the home-test kit into the
always-installed `jbrain` behavior pack and a new `jbrain` resource pack, which the
wrapper installs into every world it loads and BDS pushes on join
(`texturepacks-required=true` is pinned). Each copy is named "<Player>'s Tricorder" with
the lore "Points where Dave says", created with `lockMode = inventory` and
`keepOnDeath = true`, and carries its owner's `player.id` and a serial as item dynamic
properties: the world keeps each owner's current serial, so a new copy retires every
older one wherever it is (it reads "This tricorder is retired", and is unlocked so it
can be thrown away). Only the owner sees the arrow; with no waypoint it points at world
spawn (the pod's hatch in a crash-site world). The stable script API has no xuid, so the
owner key is the player's entity id, which BDS keeps per player per world. Waypoints
still go by gamertag, as Dave's tool sends them, and its replies are now `[jbrain]
waypoint …` lines. The rules are pure functions in `jbrain-pack/scripts/rules.js`, tested
under node (`deploy/minecraft/jbrain-sim/`, run by the supervisor's pytest). Still to
come: Dave handing one over in the welcome (DW1), the arrival chime (both since built), and a target set
by `guide_me`. **Box checks:** the lock in Bedrock's own UI on Windows and Xbox (the
drop key, dragging it out, a chest, a death); that `player.id` is stable across
sessions; the icon download on join; home test rows 5, 10–12.

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
- **Getting one:** ~~Dave hands it over the first time he sets a target; optionally a
  crafting recipe~~. **Superseded by the canon (DW1):** it's beamed down in the welcome,
  and again at a respawn without one. It can't be crafted.
- **It's personal (owner, 2026-10-10).** It is named **"<Player>'s Tricorder"** (e.g.
  "Steve42's Tricorder") and points to **its owner's** target, whoever holds it, so a
  sibling can carry it to lead the way. **Superseded by the canon (DW1):** a Tricorder
  only works for its owner. Anyone else holding it gets static, with no arrow and no
  Dave. The owner's id is an item dynamic property (the
  item doesn't stack, so it can hold one).
  - Each player has one at a time. A replacement from Dave retires the old one, which
    then reads "This tricorder is retired".
- **Never dropped on death (owner, 2026-10-10).** The item is created with
  `ItemStack.keepOnDeath = true` (stable API), so it stays in the inventory through a
  death even when everything else drops. M10's drop path skips it too.
  - It can still be thrown away, put in a chest, or burnt in lava like any item.
    **Superseded by the canon (DW1):** it's locked to the inventory, so it can't leave
    the player at all, and a missing one is re-beamed whenever they wake.

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

**Remaining (checked against the code, 2026-10-13):** the item, ownership, the arrow,
waypoints and saved places are built, and DW1 hands it over in the welcome and re-beams
it. The arrival chime is built (2026-10-13, above), and the Nether pointer aiming at a
known portal shipped with §M4e (2026-10-13). Left: the box checks above. A
separate `guide_me` is not needed: `mc_set_waypoint` is Dave's target.

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

A **Power Pack** is crafted from **8 Ender Pearls in a ring around 1 Blaze Powder** on a
crafting table (owner, 2026-10-11; it was nine Eyes of Ender until then). The pearls come
from hunting endermen and the powder from a Nether fortress's blazes, so packs arrive once a
player has reached the Nether. Plain Ender Pearls still throw and teleport as usual; Eyes of
Ender stay the End-portal key and nothing else.

**How it's made:**
- A **custom item `jbrain:power_pack`**:
  - Name "Power Pack", plus lore: *"The same fuel the Old Makers' ships ran on."* (owner,
    2026-10-12; the earlier draft was "Keeps your things, or takes you somewhere").
  - Max stack 16.
  - **Its own icon**, set A of `docs/mocks/minecraft-items/` (a copper-capped glass cell,
    with a glint), from the server-pushed `jbrain` resource pack (§3c rule 2).
  - It isn't a pearl or an eye to the game, so it can't be thrown or used to fill an
    End-portal frame by accident.
- A **shaped recipe** (`recipes/power_pack.json`, crafting table, pattern
  `PPP / PBP / PPP`, P = `minecraft:ender_pearl`, B = `minecraft:blaze_powder`, unlocked in
  the recipe book by an Ender Pearl → one `jbrain:power_pack`) in the behavior pack. Recipes are
  stable data and need no experiment, and it shows in the recipe book like any vanilla
  recipe.
- **Checked in M0b:** the item shows with its icon and name, and the recipe works, on the
  Xbox and on Windows, after the automatic download on join.
- **Fallback, only if that download fails on a client:** Dave "forges" a pack instead of
  the crafting table. "Dave, make me a Power Pack" takes 8 pearls and a Blaze Powder and gives an Ender
  Pearl named "Power Pack" with lore. The script recognises it by its lore (an anvil can
  rename an item but can't add lore, so it can't be faked).

#### Keep your inventory, once per Power Pack

**The rule.** A player who dies with a **Power Pack** anywhere in their inventory keeps
everything, and **one pack is used up**. **Canon (DW1, owner 2026-10-11):** they also need
**their own Tricorder** anywhere on them. In the story, the Tricorder draws on the pack to
hold a field the transporter locks onto. Without one, the Power Pack does nothing. Without one, the death is a normal Survival death:
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

**Shipped (wave mc-items, 2026-10-11): the keep-inventory charm.** `jbrain:power_pack`
and its recipe are in the `jbrain` pack (out of the kit), and the switch is
built: the slot's `power_pack_keep`, **off by default**, set by
`PUT /api/minecraft/worlds/{slot}/power-pack` (owner) or
`PUT /api/debug/minecraft/worlds/{slot}/power-pack`. The
sidecar holds `keepInventory` on while it's on (the rules view reports it under
`managed` and refuses a change to it). Turning it on pins the value the world had as the
owner's saved choice when none is saved (an import, a reset), so turning it off hands back
exactly that, saved so a later load agrees. It tells the pack with `scriptevent jb:config {"power_pack_keep": …}` after the
rules at every start and at once on the loaded world; the pack keeps it as a world
dynamic property. On a player's death with the switch on and the rule on: a Power Pack
(inventory, armor or off-hand) and their own Tricorder burn one pack, smallest stack
first, keep everything, and tell them on respawn with a fizz; anything less drops the
inventory, armor and off-hand at the spot (the last place they stood for a void
death), skipping keepOnDeath items, destroying Curse of Vanishing, and resetting XP with
7 orbs a level up to 100. A fault in the charm leaves everyone keeping (logged).

**Shipped (wave mc-keep-inventory, 2026-10-11): the PWA toggle.** Through the GUI gate
(`docs/mocks/minecraft-keep-inventory/`, owner pick **A** with the review's conditions)
the rules editor's **Keep inventory on death** row is a segmented **Off · On · Power Pack**
rather than a switch on the world's page: Off and On write the plain rule, Power Pack writes
the slot's switch, and leaving Power Pack turns the switch off before any rule write. While
it's on, the rule is excluded from every changed count and reset. The New world form offers
Off · On only. DESIGN.md "Worlds and backups" has the rest.

**Built (2026-10-12): the Power Pack's lore.** A recipe can't set lore, so the pack's
once-a-second look at each player's inventory (main.js `learn()`) writes
`LINES.powerPackLore`, *"The same fuel the Old Makers' ships ran on."* (owner,
2026-10-12), on every Power Pack they carry that lacks exactly that line, keeping its
amount and everything else. Every pack gets the same single line (an older wording is
rewritten too), because Bedrock stacks only identical lore. A fresh craft lands apart
from a lored stack, and Bedrock never merges two stacks already in separate slots, so the
look folds a newly lored inventory pack into any lored stack with room (to 16); only what
doesn't fit keeps its own slot, and nothing is lost (#1694). The fold writes the source slot
first and the top-ups after it; a write that throws (a player mid-leave) puts the source's
unmoved packs back as they were, so a failure never makes a pack and the next look folds
them again (#1698). A pack in a chest gets it when it's next carried. Transport shipped later, below.
**Box checks:** home
test rows 3, 4, 13–16, which also answer the checks above (the inventory writable at
`entityDie`, `spawnItem` keeping enchantments, names and durability, and what one
summoned `xp_orb` is worth, so the orb count can be tuned).

**Why it's costly:** one death saved costs 8 Ender Pearls and a Blaze Powder, so it's a real decision, not a free
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
- 8 Ender Pearls around a Blaze Powder make one Power Pack; 7 pearls, or no powder, make nothing.

#### Teleport to a known place, one Power Pack per trip (owner, 2026-10-10)

**Canon (owner, 2026-10-11): this is endgame transport on request.** Dave transports
the asker when they ask, as long as they have their own Tricorder and a Power Pack. It's
the one acting tool the AI holds; everything else it does stays read-only. The pack
makes the move, only for the asker, after checking the Tricorder and pack, and uses up
one pack. The story and words are on the lore page. It isn't part of M10's first build.

**The rule.** A player who has settled a place with Dave can ask to be **teleported there**.
It costs **one Power Pack** from their inventory. With none, Dave says so, and offers
the tricorder (M9) instead. **Canon (DW1):** it also needs their own Tricorder anywhere on
them, which in game it always is, since Dave is only reachable through one.

**Only the player's own saved places (canon, owner 2026-10-11).** A transport goes only to
a place the asker has saved with `mc_place_save` (`app.mc_places`), such as "home" or
"tree farm". It never goes to a bed, a located structure, a coordinate said in chat, or
another player's place. To go somewhere new, the player saves it first. ~~The earlier
list of other "known" places (beds, shared waypoints, confirmed `locate` results)~~ is
superseded.
- **Saving marks where the player is standing (owner, 2026-10-11).** `mc_place_save`
  takes no coordinates. In game it saves where the asker stands. From the PWA it saves
  the player's current position only while they're online, from a tracker sample less than
  a minute old, and otherwise refuses. So a saved place, and any transport, is always
  somewhere they've been.

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

**Why it stays fair:** each trip costs a Power Pack (8 Ender Pearls and a Blaze Powder). The same packs are the
keep-inventory charm, so players choose how to spend them.

**Tests:**
- No pack: refused, nothing changes.
- Blocked landing: refused, and the pack is kept.
- Success: exactly one Power Pack removed and the player is at the spot.
- An unconfirmed `locate` result is refused as unknown.
- A cross-dimension trip to a dimension never visited is refused.
- Choosing No on the form costs nothing.

**Shipped (wave mc-transport, 2026-10-11): transport on request, without its PWA toggle.**
- **The tool.** `mc_transport {place}` (`agent/minecrafttransport.py`), Minecraft_Dave's
  one acting tool. It works only on an in-game turn, only for the asker, and only to a
  place in their own `app.mc_places` for the loaded world, never a bed, a structure,
  coordinates or someone else's place. It's offered only once the asker has unlocked
  `power_pack_uses`: before that the door leaves it out of the turn, so its description
  can't name the pack (§M19).
- **The pack decides.** The tool sends `scriptevent jb:transport {id, player, label, x, y,
  z, dim, visited}`, where `visited` is the dimensions the travel log has seen the player
  in, in this world. The pack's own `transport.js` (pure rules in `transport_rules.js`)
  also keeps `jb:visited:<id>` once a second. It refuses, spending nothing, when:
  - the switch is off;
  - the player has no own Tricorder, or no Power Pack (inventory, armor or off-hand);
  - the place has no y;
  - the place is in a dimension they've never been in;
  - they're offline, or a form is already open for them.
- **The form.** Otherwise a `@minecraft/server-ui` `MessageFormData` asks Yes or No.
  - That's server-ui **2.0.0**, stable beside `@minecraft/server` 2.0.0. The manifest
    declares it, so no experiment is needed.
  - The text is the plan's own: `Market — 1,239 blocks, Overworld. Use 1 Power Pack? (you
    have 3)`, with no distance for another dimension.
  - A busy player (chat still open) is asked again each second.
  - The offer lapses after 45 s.
- **On Yes.** The pack loads the landing with a temporary `tickingarea` (up to about 5 s),
  then refuses ground that never loads and lava at the feet or head. It checks the switch,
  the Tricorder and the pack again, calls `tryTeleport` with `checkForBlocks`, and only if
  that lands burns one pack, smallest stack first. The enderman-teleport sound and portal
  particles play at both ends.
- **Every step is one console line,** `transport <id> <outcome> {json}`. The tool reads the
  offer back at once, then reads `/logs` for up to 50 s for how it ended: moved, declined,
  expired, blocked, unloaded or refused.
- **The trip log.** A trip, or a landing that stopped one, goes in the player's progress
  log (`app.mc_goal_log`, source `auto`): from, to, and the pack spent or kept.
- **The switch.** "Power Pack teleport" is the slot's `power_pack_transport`, on by
  default. It travels in the same `jb:config` line as `power_pack_keep`, and an older
  record reads as on. It's set by `PUT /api/minecraft/worlds/{slot}/power-pack-transport`
  (owner) or `PUT /api/debug/minecraft/worlds/{slot}/power-pack-transport`. The PWA
  toggle shipped later, below.
- **Dave's words.** The guide has a `power_pack_transport` entry (guide v3), shown only
  where the switch is on. It reveals that saved places are transport beacons, the
  first-Power-Pack beat's canon. The persona (`agent-minecraft-dave-v12`) says "transport
  beacon" only once that entry is there. M18's fast path still leaves "take me to …" to
  the model.

**Signed off (owner, 2026-10-12):** the form's Yes/No line and Dave's transport replies.
**Built (2026-10-12):** the Power Pack's item lore (above).

**Shipped (wave mc-teleport-switch, 2026-10-12): the PWA toggle.** Through the GUI gate
(`docs/mocks/minecraft-transport-switch/`, owner pick **B**) "Power Pack teleport" is a
switch row under Cheats in the world page's Settings, built as Cheats is, with a fixed help
box under it; the New world form has the same switch (on by default), so the create body
takes `power_pack_transport` (left out, it's on). It isn't a game rule: the rules editor's
counts and resets never touch it. The Worlds list says *teleport off* only for a world that
has it off. DESIGN.md "Power Pack teleport" has the rest.

**Box checks (after deploy):**
- The form shows over the game on the Xbox and Windows, and Yes is `selection` 0.
- `tryTeleport` with `checkForBlocks` refuses a built-over spot.
- The `tickingarea` preload reaches a far place, and leaves no ticking area behind.
- The portal particle id draws.
- One pack is gone after a trip and none after No.
- A Nether place before and after the player's first Nether visit.
- The trip line in their progress log.

### M11 — Trans-dimensional chests (owner idea, 2026-10-10; details open) ⏸️ On hold

**Status: On hold (owner, 2026-10-12).** Nothing here is scheduled; the design below
stays as written for when the owner picks it up again.

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
   (8 pearls and a Blaze Powder), and the dye is free. Or the chest is cheap but each **new colour channel**
   costs a Power Pack the first time it's used.
3. **How many colours:** the 16 dye colours.
4. **What happens to a vault if its owner is removed from the allowlist:** it's kept, and
   the owner can see its contents from the PWA.

### M12 — Wormhole gates: a seed, a diamond gate, and Dave (owner, 2026-10-11; shipped 2026-10-11)

**Shipped (2026-10-11).** Built as decided below, in the jbrain pack's own module
(`jbrain-pack/scripts/wormhole.js`, rules in `wormhole_rules.js`; tests
`jbrain-sim/wormhole*.test.mjs`, run by `supervisor/tests/test_minecraft_wormhole.py`), the
seed's item, recipe and icon, the `jbrain:wormhole` surface block (a 16-frame flipbook,
art in `docs/mocks/minecraft-items/make_wormhole.py`), and one Dave tool,
`mc_wormhole_aim` (`backend/src/jbrain/agent/minecraftwormhole.py`), in game and in the app.
What the build settled:
- **One tool opens and re-aims.** The first opening of a gate is free; every aim after it
  costs a Power Pack, including re-opening a gate that went dormant. Aiming at the place it
  already leads to changes and costs nothing. The backend checks the place is the player's
  own (`app.mc_places`, loaded world) and its dimension visited (the Overworld always, the
  dimension they stand in, or any in their travel log); the pack checks the rest live
  (online, within 4 blocks of a gate they planted, the ring clear, the pack in hand or
  inventory or off-hand, the smallest stack burnt) and answers on the console, read back
  as `mc_set_waypoint` does.
- **The frame is the ten ring blocks.** Corners are optional, so a seed can't be planted
  from one and breaking one leaves the gate standing; breaking any of the ten collapses it
  (the record and the surface go, nothing drops). A player's break collapses it at once;
  pistons, blasts and commands are caught by a once-a-second look at gates in loaded ground.
- **Records** are world dynamic properties (`jb:gates`, `jb:gate:<id>`), keyed to the
  planter's gamertag (the pack has no xuid, as with waypoints). A gate keeps the place's
  coordinates from when it was aimed: saving the place again elsewhere doesn't move it until
  it is re-aimed; removing the place (`mc_place_remove`) takes the planter's gates that led
  there dormant.
- **Trips are logged** as `[jbrain] wormhole trip: …` lines in the server log (plantings,
  collapses and refused landings too). Mirroring gates and trips to the sidecar for the PWA
  and the M8 map waits for M8.
- **Players only** travel (mobs and items stay an open option). A trip happens on stepping
  into the surface; a blocked landing (`tryTeleport` with `checkForBlocks`) is refused once,
  with a message, and tried again only after stepping out and back in.
- **The item guide** gains the seed (`wormhole_seed`): the pack unlocks it on a first popped
  chorus fruit once the player has made a Power Pack, and in a world with neither the keep
  nor the transport switch the Power Pack's "nothing yet" entry gives way to it. In game the
  door offers `mc_wormhole_aim` only with that unlock, as it does transport and the
  Replicator's tool, so nothing in an earlier turn names the gates.
- **Messages are plain status**, not Dave's lines, until the lore page has his words for
  planting and opening (below).
- **Still to check on the box:** that using the seed on a diamond block fires
  `playerInteractWithBlock` on Bedrock (a custom item with no use behaviour), the flipbook
  and blend render of the surface, the particle and sound ids, and `tryTeleport` into an
  unloaded chunk or another dimension.

**Decided (owner, 2026-10-11).** This replaces the 2026-10-10 proposal of paired seeds
and twin gates. A wormhole works like transport on request (M10): a crafted **Wormhole
Seed** is planted in a **diamond gate**, and **Dave opens it** to one of the planter's own
saved places.
- **One-way.** A gate leads to its saved place and nothing comes back through it. A return
  trip needs a second gate (a second seed and frame) at the other end, which is the point:
  it doubles the cost.
- **Anyone may step through** (owner, 2026-10-11): a gate is shared infrastructure. Opening
  and re-aiming stay the planter's, to their own saved places.
- **Re-aimable.** Standing **within 4 blocks of the gate**, the player asks Dave to point
  it at another of their saved places. Each re-aim costs **one Power Pack**.
- **The cost** is the seed's craft, plus the frame's diamonds, plus a Power Pack per
  re-aim. Opening a gate the first time costs nothing beyond the seed.

**How a gate comes to be:**
1. **Craft a Wormhole Seed** (`jbrain:wormhole_seed`, stack 1): the **chorus seed** (owner,
   2026-10-11), a Power Pack in the centre, 4 popped chorus fruit on the sides and 4 Ender
   Pearls in the corners. Chorus grows only on the outer End islands, so wormholes come
   after the Ender Dragon: the No-Spawn Beacon is mid game, wormholes are late, the
   Replicator is endgame.
2. **Build the frame:** a Nether-portal-shaped ring of **blocks of diamond**, 4 wide by 5
   tall, with or without corners (10 blocks, 90 diamonds, at the least). The frame is
   checked by the script, so a bigger frame is a later option, not a guess.
3. **Plant the seed:** use it on the frame. The script checks the shape, takes the seed and
   records the gate as **dormant**, with a faint shimmer and Dave's line.
4. **Ask Dave** within 4 blocks of the gate: "Dave, open the wormhole to *tree farm*". He
   checks:
   - the asker is within 4 blocks of a gate **they** planted;
   - the place is **one of their own saved places** (`app.mc_places`), the same rule as
     transport;
   - the place's dimension is one they have already visited.
   Then the gate fills with the wormhole surface, a custom block from the `jbrain`
   resource pack.
5. **Step in:** the 5-tick loop sees a player inside the surface and `tryTeleport`s them to
   the saved place, with `checkForBlocks`. A spot built over or flooded since is refused,
   and the player stays put.

**Re-aiming** follows the same checks as opening, and the pack removes one Power Pack only
once the gate has really changed. With none, Dave says so and the gate keeps its target.

**Rules:**
- **Breaking a frame block destroys the gate, and the seed is lost** (owner, 2026-10-11).
  Rebuilding the frame needs a new seed. While a gate stands, its planter can ask Dave to
  re-point it (above).
- **Every trip is logged** (who, gate, place) and gates show on the M8 map — built in
  §M26: each gate, open or dormant, and a dotted line to where an open one leads.
- **Saved places stay the planter's.** If the target place is removed (`mc_place_remove`),
  the gate goes dormant until it is re-aimed.

**Built on:** the custom seed item, the frame check by block reads, a custom surface block,
`tryTeleport`, and gate records in world dynamic properties (gate id, frame position and
axis, planter xuid, target place, state), mirrored to the sidecar for the PWA and the map.
Opening and re-aiming are Dave tools on the M10 path: a `scriptevent` the pack carries out,
and the pack's answer read back, as `mc_set_waypoint` does.

**Open (owner):**
- **Whether mobs and items travel** (later option).
- **Lore:** the story and Dave's lines for planting, opening and re-aiming belong on the
  Bleepbloop_City lore page, as a later-tech beat shown once gates are built.

### M13 — The laser cannon: a beam weapon (owner idea, 2026-10-10; details open) ⏸️ On hold

**Status: On hold (owner, 2026-10-12).** Nothing here is scheduled; the design below
stays as written for when the owner picks it up again.

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

### M14 — Powered armor: netherite plus a Power Pack (owner idea, 2026-10-10; details open) ⏸️ On hold

**Status: On hold (owner, 2026-10-12).** Nothing here is scheduled; the design below
stays as written for when the owner picks it up again.

**The recipe (owner):** a **netherite armor piece + a Power Pack** gives the **powered**
piece: powered helmet, chestplate, leggings and boots.
- **Made at the smithing table, not the crafting table.** Bedrock's
  `recipe_smithing_transform` (behavior-pack data, stable) is how vanilla upgrades diamond
  to netherite, and it **keeps the piece's enchantments and damage**. A crafting-table
  recipe would wipe a player's enchantments.
- **The slots:** template = netherite upgrade smithing template, base = the netherite
  piece, addition = a Power Pack. A full set costs 4 Power Packs (32 Ender Pearls, 4 Blaze Powder) plus 4
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

### M15 — The Replicator: a furnace Dave programs to make one thing (owner, 2026-10-11; shipped 2026-10-11)

**What and why.** The story and rules are canon in Bleepbloop_City's
[`notes/lore.md`](https://github.com/jeffmhopkins/Bleepbloop_City/blob/main/notes/lore.md#the-replicator).
A furnace surrounded by 8 Power Packs crafts a blank **Replicator**. Dave programs it from
8 full stacks of a target, which he takes as the pattern. After that it burns any input
block into one of the target, about every 10 s, on normal fuel. This section is only how
jbrain2 builds it. It needs M10's Power Pack item, and it's endgame.

**How it's built** (stable `@minecraft/server` only, in the `jbrain` pack):
- **The item.** `jbrain:replicator` is a custom item (stack 1, its icon in the `jbrain`
  resource pack). A shaped recipe in the behavior pack takes a furnace in the centre and
  `jbrain:power_pack` in the other 8 slots. Each slot takes one, so a normal recipe works.
  The programmed target is an **item dynamic property** (`target`, an item id). A
  programmed one is named "<Target> Replicator", for example "Iron Replicator".
- **Programming** is a Dave tool, `mc_replicator_program(target)`, in game only. It sends
  a `scriptevent` that the pack handles for the asker alone:
  - the asker holds a Replicator in the selected slot, blank or programmed;
  - their inventory holds at least 512 of `target`, and `target`'s max stack size is 64;
  - then the pack removes the 512, sets the property and renames the item, and Dave
    confirms;
  - otherwise nothing changes, and Dave says what's missing.
  - It's an acting tool like transport on request: the pack checks everything, it only
    touches the asker's own inventory, and the AI never moves items any other way.
- **Placing.** On `playerInteractWithBlock` with a Replicator, or its placement, the pack
  puts a **vanilla furnace** at the target spot. It records `{x, y, z, dim, target}` in a
  world dynamic property, a list of replicators, and consumes the item.
  - A vanilla furnace keeps the furnace UI and hopper behaviour for free.
- **Running.** A `system.runInterval`, with its timing on the clock as the wreck's waits
  are, visits each recorded replicator whose chunk is loaded, and every ~10 s:
  - takes one item from the input slot and one fuel item, honouring the fuel's burn time
    so coal lasts its 8 items;
  - adds one `target` to the output slot when there's room.
  - Vanilla smelting may race it for smeltable inputs; the pack drains the input first,
    and a box check confirms the furnace's container slots behave.
- **Breaking.** On `playerBreakBlock` of a recorded furnace, the pack cancels the furnace
  drop, spawns the programmed Replicator item, with its target, at the spot, and forgets
  the record. An explosion or piston is handled the same way where the API reports it.
  Otherwise, a missing furnace found on the visit pass forgets the record.

**Open (owner):** none in the canon. To check on the box: the furnace container's slot
order, cancelling a furnace's drop, and the pace at which an empty server runs the
interval.

**Tests.** Node tests against the fake server cover the recipe's shape, programming
(enough, too few, a 16-stack target, an unstackable target, reprogramming), the run loop
(fuel burn time, a full output, an unloaded chunk) and breaking (the item returns with its
target). The Dave tool gets the usual tool tests.

**Shipped 2026-10-11 (wave mc-replicator).** Built mostly as above; where it differs, the
build wins and the reasons are here.
- **A custom block, not a vanilla furnace.** The item design
  (`docs/mocks/minecraft-items/README.md`, set A, binding) makes the Replicator a placer
  item for its own block, `jbrain:replicator`, with the set's faces, a `programmed` state
  that lights the front and top, and `minecraft:placement_direction` facing. Putting a
  vanilla furnace down instead would show a plain furnace and drop a plain furnace.
  - **What stable `@minecraft/server` 2.x can't do:** a custom block can't have a
    container or a furnace screen, and a script can't open one on it. Hoppers can't push
    into it or pull from it.
  - **So the "furnace" is the containers round it:** a chest, barrel or hopper **on top**
    holds the blocks it burns (any item that is also a block), one on **any side** holds
    the fuel, and one **below** takes what it makes. Those are the faces a furnace's
    hoppers use, so a hopper on top feeding down, one beside it, and one below carrying
    the output away all work. Canon's "fuel in the bottom" is the furnace screen's bottom
    slot; here the bottom is the output.
  - **Decided (owner, 2026-10-11): furnace layout — top input, side fuel, bottom
    output, hoppers on each face like a vanilla furnace.** Chests and barrels work too. A
    hopper on top pointing down, or beside it pointing in, can't push into the block, so
    its items wait in it and the script pulls them; a hopper below gets the output and
    passes it on.
  - Using the block (right-click) opens a small `@minecraft/server-ui` panel (owner
    request, 2026-10-11): titled "Iron Replicator" or "Replicator — not programmed", with
    what it makes, what it's waiting for right now, and how to connect each face. It
    opens no panel while the player sneaks (that's placing a hopper or chest on it, as on
    a furnace), never stacks a second one, and falls back to chat when a form can't open.
    Its wording is a draft until signed off on the lore page.
- **Working look (owner request, 2026-10-11).** A `jbrain:working` state beside
  `jbrain:programmed` gives three looks: blank, programmed-idle and programmed-working.
  Working swaps the front window and top centre for 8-frame flipbooks (an amethyst pulse
  and an ember glow, `make_replicator_working.py`) and gives light 10. It turns on on a
  tick that makes something and off on the first that doesn't; unloaded, it keeps its
  last look. Each item made puts a flame and an amethyst sparkle at the front and plays a
  furnace crackle; a particle or sound the game refuses never stops the making. No idle
  crackle between items: the block only ticks every 10 s. The item sprite stays the flat
  programmed one.
- **The run is a block tick, not an interval over a list.** The block's
  `minecraft:tick` fires every 200 game ticks (10 s), and the game only ticks loaded
  land, so canon's "only while its land is awake" is the engine's own rule, and nothing
  catches up afterwards. Each tick: room below, a block on top, and fuel (a new fuel item
  is lit only when under 200 ticks remain, using vanilla burn times: coal 8, a coal block
  80, lava 100 and its bucket left behind) make one target. Wanting any of them, it waits
  and takes nothing.
- **Programming** is `mc_replicator_program(target)` (`agent/minecraftreplicator.py`, in
  game only, in both of Dave's tool sets): one `scriptevent jb:replicator` with the
  asker's gamertag and a checked `minecraft:` id; "iron", "gold", "copper" and
  "netherite" mean their ingots. The pack refuses (not holding a Replicator, no such
  item, a stack size other than 64, the same target again, fewer than 512) or takes
  exactly 512 from the inventory and swaps the held item for
  `jbrain:replicator_programmed` named "<Target> Replicator", lore *Burns any block into
  <target>*, target in the item property `jb:target`. That second item exists only so the
  slot draws the programmed sprite (an item's icon can't change per stack); both place
  the same block. Prompt `agent-minecraft-dave-v13`.
- **Placing and breaking.** On placement the held item's target goes into a world
  property per block (`jb:replicator:<dim>:<x>,<y>,<z>`, with fuel left and its state);
  the block's loot table is empty and breaking spawns the same item back, programmed as
  it was (also in creative). Fuel burning inside is lost, as a furnace's is. It is
  immune to explosions and immovable by pistons, so neither can lose its programming.
- **The guide (§M19):** a player with 8 Power Packs on them unlocks `replicator` (the
  pack checks once a second); its entry is in `agent-minecraft-dave-guide-v4`.
- **Packs:** behavior pack 1.3.0 and resource pack 1.2.0, so clients fetch the new
  icons and faces.

Only checkable on the box: that the block loads (custom components on format 1.21.90+,
`minecraft:movable`, `replace_block_item`), that its front faces the player in all four
directions and the GUI shows the flat sprite, that it keeps ticking after a restart,
that a hopper below takes the output on, that breaking it gives the named item back, and
that "Dave, program this replicator for iron" works with 512 ingots, that the working faces animate and
light the room only while it makes things, that the flame, sparkle and crackle play at the
front in every facing, and that the panel opens on a phone and an Xbox.

### M16 — The No-Spawn Beacon: a mob-free 3×3 of chunks (owner, 2026-10-11; shipped 2026-10-11)

**Why.** The owner's endgame is a base out "in space": high in the sky or out over the
End. Endermen would still come, because they teleport in. A beacon keeps a whole area
clear without a global rule change (Bedrock's only switch, `doMobSpawning`, is
world-wide).

**Decided (owner, 2026-10-11):**
- **Reach:** the **3×3 block of chunks** centred on the beacon's own chunk, the full
  column from the world's floor to its ceiling. That is 48×48 blocks, so a base across a
  chunk border is covered. Overlapping beacons are fine.
- **No Dave to switch it on:** crafted and placed, it works at once. Breaking it switches it
  off. **Anyone can break it, like any other block** (owner, 2026-10-11), and it drops
  itself.
- **What it removes:** hostile and neutral mobs, and wild passive mobs nobody has
  interacted with (below).
- **Recipe (mid game):** a **Power Pack surrounded by 8 echo shards** on a crafting table,
  a shaped recipe in the behavior pack. Echo shards mean raiding an ancient city, so a
  mob-free base is earned before the Wither, not after it.
- **How:** the mob **vanishes**, with a puff of particles and no drops. In the story Dave
  beams it away.

**What is kept** (proposed from the owner's "or maybe just non-interacted also"):
- players;
- anything **tamed** (a wolf, cat or parrot), **name-tagged** or **on a lead**;
- anything a player has **interacted with**: `playerInteractWithEntity` (stable) sets an
  entity dynamic property `jb:kept`, so a villager traded with or a cow that was fed stays;
- animals **bred** by players (`entitySpawn` with cause `Born`);
- villagers and golems (iron, snow and copper golems, which canon calls Old Maker helpers);
- bosses: the Ender Dragon and the Wither are never removed, so a beacon can't skip a
  fight. **The Warden is removed** like any other hostile mob (owner, 2026-10-11).

Everything else in range goes: zombies, skeletons, creepers, spiders, phantoms, slimes,
endermen, piglins, wild cows and sheep, and the rest.

**How it's built** (stable `@minecraft/server`):
- **A custom block** `jbrain:no_spawn_beacon`, with its own texture from the `jbrain`
  resource pack, and the shaped recipe above.
- **Placing** it (`playerPlaceBlock`) records it in a world dynamic property: dimension,
  position, and the 9 chunk keys it covers. **Breaking** it (`playerBreakBlock`, or the
  block missing on a sweep) removes the record.
- **Spawns:** on `entitySpawn`, a mob whose chunk is in the protected set and that isn't
  kept is removed at once (`entity.remove()`), with particles. A set lookup, so it costs
  almost nothing.
- **Arrivals:** once a second, a sweep reads the mobs in each protected area
  (`dimension.getEntities` with a volume) and removes any that aren't kept. This catches
  endermen that teleport in, and anything that walks, falls or comes through a portal.
- **Only where it matters:** mobs exist only in loaded chunks, so the sweep skips areas no
  player is near.
- **Map and Dave:** protected areas show on the M8 map, and Dave can say which of a
  player's places are covered. Neither is needed for the beacon to work.

**Side effects to know:** inside the area, raids, phantoms and mob farms stop working. A
mob farm needs to be outside the 3×3.

**Open (owner):**
- **Lore:** Dave's first-time line for the beacon belongs on the Bleepbloop_City lore page
  (its story is there now, "The No-Spawn Beacon"); none is built.

**Shipped 2026-10-11 (wave mc-beacon).** The block `jbrain:no_spawn_beacon`
(`jbrain-pack/blocks/`), its shaped recipe (`recipes/no_spawn_beacon.json`, unlocked in the
recipe book by an echo shard), and its logic in its own module, `scripts/beacon.js`
(rules in `beacon_rules.js`); `main.js` only calls `initBeacon(world, system)`. What was
decided while building it:
- **Kept is a list, removed is a list.** The beacon removes only the vanilla hostile,
  neutral and passive mob types it names (`REMOVABLE`); any other entity (items, XP,
  arrows, boats, armor stands, another pack's entities, a mob the game adds later) is
  never touched. Always kept: players, villagers (both ids), iron, snow and copper
  golems, the Ender Dragon and the Wither. The wandering trader and his llamas are
  `VISITORS` (owner, 2026-10-12): banished like wild mobs, and a trade (an interaction)
  doesn't keep one; only a lead or a name tag does. Any removable mob stays
  when tamed (`minecraft:is_tamed`), name-tagged, on a lead, or marked `jb:kept`.
- **`jb:kept`** is set on a removable mob by any `playerInteractWithEntity` (feeding,
  trading, a name tag being put on), and on every removable mob born (`entitySpawn` cause
  `Born`), wherever it is: a bred animal carried or led into the area later stays.
- **Records:** one world property per beacon, `jb:beacon:<dimension>@x,y,z`, so any
  number fit. Read lazily on first use (a script can't read world properties while it
  first loads) and rebuilt into a per-dimension set of covered chunk keys.
- **The sweep** runs every 20 ticks over each beacon whose block reads back: an unloaded
  beacon chunk is skipped, and a beacon block that's gone without a player breaking it
  (an explosion) drops its record. It queries the 48×48 full-height volume, excluding
  players. Each removal is `entity.remove()` (no death, so no drops) after an end-rod
  sparkle and a smoke puff.
- **Not done:** pistons can move the block (its record then drops on the next sweep and
  the moved block does nothing until it's broken and placed again). The M8 map and Dave
  know about beacons since §M26 (the protected squares, and `mc_gates_beacons`' covered
  check).
- **The guide** (§M19): an echo shard unlocks `no_spawn_beacon` only for a player who has
  already unlocked `power_pack_uses` (`UNLOCK_NEEDS` in `rules.js`; both in one look
  counts). The entry is in `minecraft_dave_guide.prompt`, `agent-minecraft-dave-guide-v2`
  (re-pinned; v3 adds transport, §M10), and in the owner's chats.
- **Icon:** set A, drawn by `docs/mocks/minecraft-items/make_beacon.py`: a tuff block in
  a copper frame; the top is the recipe from above (eight echo shards round a lit
  amethyst lens), the side an amethyst column with echo-teal lights. The faces ship in
  `jbrain-rp/textures/blocks/` through a new `terrain_texture.json`; the resource pack's
  version went to 1.1.0 so clients fetch the new textures.

Only checkable on the box, by a player:
- the recipe crafts (a Power Pack ringed by 8 echo shards) and shows in the recipe book
  after an echo shard; the block places, shows its faces, glows faintly, and drops itself
  when broken by hand;
- placed at night, hostile mobs stop appearing in the 48×48 square round its chunk, top to bottom, and a
  wild cow walked in vanishes in a puff with no drops, while a named, leashed, fed or bred
  animal, a tamed wolf, a villager and an iron golem stay; an enderman lured into a sky
  base vanishes within a second;
- broken, mobs spawn there again; the console shows `no-spawn beacon on at …` and
  `… off at …`;
- the particle ids (`minecraft:endrod`, `minecraft:basic_smoke_particle`) render, and
  "what's a no-spawn beacon?" is answered only after a made Power Pack and an echo shard.

### M17 — Deep-scan limit: two locates per player per Minecraft day (owner, 2026-10-11; shipped 2026-10-11)

**Why.** Dave's searches stay useful without making the world trivial: "where's the
nearest diamond" or "the nearest ancient city" is a treat, not a reflex. In the story, the
satellite's deep scan needs time to recharge.

**Decided (owner, 2026-10-11):**
- **Two deep scans per player per Minecraft day**, in game. Each player has their own two;
  the limit is not shared.
- **The PWA is unlimited:** the owner's own chats with Minecraft_Dave and the map's
  Structures overlay never count.
- **Sleeping resets it:** the day is the world's own day counter (`time query day`), so a
  night slept through starts a new day and two new scans.
- **A repeat is free:** asking again for something Dave already found for that player today
  (the same structure kind or block, from about the same place) answers from the first scan
  and costs nothing.

**What counts as a deep scan:** the searches that reveal something nobody has pointed out:
`mc_nearby` (the server's own `locate`), `mc_find_block`, and `mc_find_item` past 48
blocks (§M23). Free: `mc_what_is_at`, saved
places and waypoints, where other players are, a player's own trail and deaths.

**How it's built:**
- **Enforced in the tool handlers**, on in-game turns only (the door's player tier), so no
  prompt can talk past it. Out of scans, the tool answers in character without running the
  search ("…the deep scan needs to recharge, crew. Ask me again tomorrow.") and the answer
  says how many remain otherwise ("1 scan left today").
- **The count** lives in a small owner-only table (world folder, xuid, Minecraft day, scans
  used, and today's answers for the free repeat), with an RLS isolation test. It survives
  restarts.
- **The day** comes from the console (`time query day`), read when a scan is asked for.
- Dave's prompt learns the rule, so he can say how many scans a player has left.

**Lore:** the recharge story and Dave's out-of-scans line go on the Bleepbloop_City lore
page.

**Shipped 2026-10-11 (wave mc-scan-limit).** `jbrain/minecraft/scans.py` and the
owner-only table `app.mc_scans` (one row per world folder, player key and Minecraft day:
scans used, and today's results as `{tool, what, dim, x, z, text}` for the free repeat;
with an RLS isolation test). What was decided while building it:
- **`mc_locate` counts too.** The list above names `mc_nearby` and `mc_find_block`, but
  `mc_locate` is the server's own `locate` for one structure or biome, the tool Dave uses
  for "where's the nearest village?". Leaving it free would have left the limit with
  nothing to hold, and the lore counts "finding the nearest structure". So all three
  are deep scans; everything else stays free.
- **Charged only for a search that ran.** A scan is reserved before the search (one SQL
  upsert that refuses past two, so two calls in one turn can't both spend the last) and
  refunded when nothing was searched: a down server, a `locate` that found nothing to
  report ("is at block" missing from its reply), or `mc_find_block` with none of the
  names in explored ground. A search that ran and found nothing in range still counts.
- **A repeat** is the same tool with the same settings (the structure kind or block set
  in any order, and the radius and limit), in the same dimension, from within
  **256 blocks** of where it was first asked. It answers from the stored text, says it
  cost nothing and how many are left, and works even when the player is out of scans.
- **The day** is read with `time query day` at each in-game scan. When it can't be read,
  `mc_nearby` and `mc_locate` (which need the running server anyway) refuse cleanly
  without searching or charging; `mc_find_block` (which reads the world's files and works
  with the server stopped) counts against the last day recorded for that world, or a
  bucket of its own (day −1) if none ever was.
- **Old days are pruned on write:** reserving a scan deletes the world's rows for every
  other day, so a world whose clock is set back starts clean.
- **Saying how many are left:** each in-game scan answer ends "1 scan left today", and
  `mc_world_info` in game adds the asker's count, so Dave can answer "how many scans do I
  have left?" (M18's fast path takes over that reply). The out-of-scans answer carries
  the lore's signed-off line word for word.
- The owner sees the counts without a terminal at `GET /api/debug/minecraft/scans`
  (per world, player and day: used, left, results kept; never the results).
- Prompt `agent-minecraft-dave-v10`; `mc_locate` v3, `mc_nearby` v2, `mc_find_block` v2,
  `mc_world_info` v2. No new tool.

Only checkable on the box: the console's real reply to `time query day` (parsed as
"Day is N"; unit tests pin that shape from the existing `mc_world_info` test), and that
sleeping through the night advances it in the family's world.

### M18 — Dave's fast path: simple asks answered without the model (owner, 2026-10-11; shipped 2026-10-11)

**Why.** Most of what players say to Dave in game is a handful of fixed requests. Answering
those directly is instant, costs no model time, and gives the same answer every time. The
model stays for real questions.

**Decided (owner, 2026-10-11):**
- **A fast path in the in-game door** (`minecraft/door.py`), before the turn reaches the
  model. A `/jb:dave` question that **clearly** matches one of a short list of patterns is
  carried out at once with the same tools the model would call, and answered from fixed lines
  in Dave's voice.
- **Strict matching.** Only an unambiguous match takes the fast path. Anything vaguer, or
  with more in it ("take me home but avoid the river"), goes to the model as before. A wrong
  fast answer is worse than a slow right one.
- **Dave remembers.** Every fast-path exchange is written into the player's normal session
  ("In game: <gamertag>") as an ordinary question and answer, so a later model turn sees it
  ("how far is it now?" after "navigate to tree farm" just works), and the owner sees it when
  reading the session in the PWA.

**The fast-path list** (first cut):
- point me home / navigate to *place* / clear my waypoint (`mc_set_waypoint`);
- save this place as *name* / forget *name* / list my places (`mc_place_save`,
  `mc_place_remove`, `mc_places`);
- where am I?
- where is *player*?
- how many scans do I have left? (M17);
- `/jb:dave hello`, which replays the welcome (DW1; the pack answers it, below);
- where's the nearest *structure* / *block*: still a deep scan that counts toward M17's
  limit, only without a model turn.

**Built so that:**
- The patterns and their replies live in one module, each with tests for what must match and
  for near-misses that must fall through to the model.
- A fast-path action goes through the same tool handlers, with the same checks (the asker's
  own places, the scan limit, fair play), as a model turn would.
- The reply lines are on the Bleepbloop_City lore page, signed off by the owner
  (2026-10-11) with Dave's other lines.

**Order:** after the Tricorder and Power Pack wave and M17, since all three touch the door.

**Shipped 2026-10-11 (wave mc-fastpath).** `jbrain/minecraft/fastpath.py` holds the
patterns and Dave's quick-answer lines (copied from the lore page, gaps filled), with
tests for each match and for the near-misses that must reach the model. The door tries it
before the model (`DaveDoor.fast_answer`); `main.py` hands it the same handlers the
in-game turn holds, and it refuses any tool outside `MINECRAFT_DAVE_INGAME_TOOLS`. What
was decided while building it:
- **Whole-question matches only**, after dropping a leading "Dave," and a trailing "?",
  "!" or "please". A place name is a few plain words: anything with an instruction or a
  reference in it ("but", "my", "nearest", "it", coordinates) goes to the model, so
  "navigate to tree farm but avoid the river" and "navigate to the nearest village"
  both do. **"take me to …" is never matched**: in the canon it is transport, which isn't
  built.
- **No signed-off line for how it came out → the model.** A place in another dimension, a
  player in another dimension or standing beside the asker, a write the tool refused, a
  search that found nothing or couldn't run, no saved places: the question goes to the
  model as if the fast path weren't there. Doing it again is safe (pointing the Tricorder
  and saving where you stand are idempotent, a refused write wrote nothing, and a scan
  already paid for today is a free repeat).
- **"Where is *player*"** needed a tool, so `mc_players` (v2) takes an optional
  `gamertag`: online, the newest position sample in the loaded world, with the way and
  distance from the asker; offline, where they were last seen. It's held in game too,
  as the owner decided (§4, "Dave may say where other players are"). Asking where you
  yourself are is "where am I".
- **"Where am I"** is the asker's position from the event plus `mc_what_is_at`'s biome;
  with the map unreadable it still answers, without the biome.
- **"Nearest *structure* / *block*"** knows a fixed list of words (Overworld structures
  `locate` finds, as `deploy/minecraft/mapping.py` names them, and the ores and spawners
  `mc_find_block` reads). It is a deep scan through the same handler (out of scans, it
  answers the recharge line word for word), then points the Tricorder at the first hit.
  The reply is the lore's signed-off everyday answer ("From up here I see one … Hold your
  Tricorder and follow the arrow, crew.") plus the scans left; the quick answers have no
  line of their own for it. `locate` is Overworld-only, so from elsewhere a structure
  goes to the model.
- **`/jb:dave hello` never reaches the backend** (DW1, 2026-10-12). The pack answers it
  itself, replaying the welcome's Dave lines, so neither the fast path nor the model
  sees it.
- **Recorded like any exchange:** the question and Dave's line go into the player's "In
  game: <gamertag>" session as a user turn and an assistant turn carrying the tool steps
  it ran (`AgentTranscript.record_exchange`, no run row since no model ran), so the next
  model turn's history and the PWA both see it.
- The handlers return their facts beside the text (`minecrafttools.Found`) so the lines
  are filled from data, not by reading prose meant for the model; a search's first hit
  is read off its text (`first_hit`), because a free repeat answers from the stored text.
- Prompt `agent-minecraft-dave-v11`; `mc_players` v2. No new tool.

Only checkable on the box, by a player: the lines in a real game chat, and that a
question typed with the Xbox keyboard (capitals, a trailing "?") still matches.

### M19 — Dave's item guide, unlocked as each item becomes craftable (owner, 2026-10-11; shipped 2026-10-11)

**Why.** Players will ask Dave how the Old Maker items work. He should explain each one
well, but not before the player can make it: the items are found in play, not read off a
list.

**Decided (owner, 2026-10-11):** Dave knows how to use every special item, and explains an
item only once the asker **can craft it**. Until then he doesn't mention it, even if asked
directly; he can say there are things he'll tell them about when they're ready.

**When an item unlocks for a player** (like the game's own recipe book, which unlocks a
recipe when you first hold an ingredient; the first-time beats of §DW1 fire at the same
moments):

| Item | Unlocks when the player first holds | Built in |
|---|---|---|
| Tricorder | from the start (Dave beams it down) | M9 / DW1 |
| Power Pack | an Ender Pearl | M10 |
| Keep-inventory and transport (the Power Pack's uses) | a Power Pack | M10 |
| No-Spawn Beacon | an echo shard, once they have made a Power Pack | M16 |
| Wormhole Seed | popped chorus fruit, once they have made a Power Pack | M12 |
| Replicator | 8 Power Packs (canon) | M15 |

- **Built only.** An item is in the guide only once its wave has shipped in that world; a
  beat never describes what isn't built (canon).
- **Per player, per world.** The pack records each unlock in the same world dynamic property
  that records the first-time beats, so it needs no backend.

**How Dave knows:**
- **The guide.** One entry per item in the prompt material: what it is (from the canon), how
  to craft it, how to use it, and its limits. The text follows the lore page and the plan,
  and is updated in the same PR as the item's wave.
- **The asker's unlocks** ride on the `/jb:dave` event from the pack (`"known": [...]`).
  The in-game turn gets only those entries; the others are not in the model's context at
  all, so it can't leak them.
- **The fast path (M18)** and tool replies follow the same rule: no locked item is named.
- **The owner's PWA chats** see the whole guide.

**Order:** with M18 (both reshape the in-game turn), after the Tricorder and Power Pack wave.

**Shipped 2026-10-11 (wave mc-fastpath).** The entries are prompt material,
`agent/prompts/minecraft_dave_guide.prompt` (`agent-minecraft-dave-guide-v1`, pinned by
hash), read by `jbrain/minecraft/guide.py`. What was decided while building it:
- **Built entries only:** the Tricorder, the Power Pack (what it is and its recipe), and
  keeping your things on death (its one built use). Transport, the No-Spawn Beacon,
  Wormhole Seeds and the Replicator have no entry until their waves ship.
- **Keeping your things is per world:** the entry is in a player's guide only when that
  world's "Power Pack keeps inventory" switch is on (the door reads the slot record from
  `/worlds`; unreadable is off). A player who has made a pack in a world with the switch
  off gets a one-line entry instead: a pack does nothing there yet, and Dave promises
  nothing.
- **The unlocks** are a world dynamic property per player id, `jb:known:<id>` (a JSON
  list), beside the Tricorder's serial: DW1's first-time beats aren't built, so there
  was no beat record to share. The pack looks at what the player carries once a second
  on the Tricorder loop and again at every `/jb:dave`; the Tricorder is always unlocked,
  an Ender Pearl unlocks `power_pack`, a Power Pack unlocks `power_pack_uses`, and an
  unlock is never taken back. The `dave` event carries them as `known`; the sidecar
  passes through only short lowercase names (at most 16), and the door keeps only those
  it knows. An older pack sends none, which is the Tricorder alone.
- **The in-game prompt** is the persona, then the asker's entries, then the chat intro;
  the owner's PWA chats get every built entry in the same place.
- The fast path's lines name no item but the Tricorder, which every player has; no tool's
  reply names an Old Maker item.
- Prompt `agent-minecraft-dave-v11`: the guide is the only source on the items, and in
  game an item that isn't in it isn't mentioned.

Only checkable on the box, by a player: that picking up an Ender Pearl, then crafting a
Power Pack, is noticed within a second (`jb:known:<id>` grows), and the answers to "what's
a power pack?" before and after.

### M20 — Map share links: a view-only map for someone without a login (owner, 2026-10-11; shipped 2026-10-11)

**Why.** The owner wants to send the map to a player (their brother) who has no JBrain
login: open a link, see the world with their gamertag on it, and touch nothing else on the
box.

**What the owner does.** On the map, **Share this map** (a fab beside the dimension
switch) opens the Share sheet for the world being viewed: pick the gamertag the map opens
on (everyone ever seen in that world, online first), **Create link**, then **Copy link**
(or **Send…** where the phone has a share sheet). The sheet lists the live links with how
often each was opened and a **Revoke** per link. A link has no expiry; it works until
revoked. Owner decisions: the link shows **every player's** live position, travel history
and timeline in that world, not only the chosen player's; it lasts until revoked.

**What the visitor gets.** `/mc/s/{token}` mounts `McShareApp` instead of the owner app: the
same map page (`MapPage`) over the public routes, opened on the shared player (selected,
in their dimension), with Layers, Timeline, Follow and the session list. It has no back
button, no world picker, no Share, and no structure re-scan; a **View only** chip sits where
the back button is. A revoked link, or a slot that now holds another world, shows *This
map link was turned off*.

**Contract.**
- Owner (`api/minecraft.py`, owner-only like the rest): `POST /api/minecraft/shares`
  `{slot, xuid}` → the link plus its token, shown once (404 when the slot holds no world or
  the player has never been seen in it); `GET /api/minecraft/shares`; `DELETE
  /api/minecraft/shares/{id}`.
- Public (`api/minecraft_share.py`, no owner dependency): `GET /api/mc-share/{token}` (the
  world and the player it opens on; the only read that counts as a view), then
  `/map/info`, `/map/tile/{dim}/{zoom}/{tx}/{tz}.png`, `/map/point`, `/map/players`,
  `/map/structures` and `/timeline` under it. The token picks the slot; the visitor never
  names one. Every route, tiles included, 404s once the slot holds another world: the
  token works outside the viewer, so an old link must never draw the new world (#1664).
  Tiles cache the slot's folder (or that it's empty) for 5 s whichever world it is, so a
  view's burst, a stale link's included, costs one sidecar read (#1698). A slot record with
  no folder is a broken sidecar: a 502, never cached, never called a gone world.
  Rate-limited per IP (tiles in their own, wider bucket), `no-referrer`, `noindex`.
- Storage and RLS (migration `0230_mc_share_links`): `app.mc_share_links` keeps only the
  token's SHA-256, the slot, its world folder at mint, and the player. A visitor reads under
  `mc_share_context(link_id)`; the `*_share` policies on `mc_player_track`,
  `mc_player_explored`, `mc_player_events` and `mc_player_sessions` admit only rows of the
  pinned, unrevoked link's world (a session by the world of its join event). Tested in
  `tests/integration/test_mc_share_pg.py`.

Only checkable on the box: that the link opens from outside the house (it needs the
public hostname — Cloudflare Tunnel or direct mode, not LAN-only).

### M21 — The Tricorder's target on the map (owner, 2026-10-12; shipped 2026-10-12)

**Why.** When Dave locks a player's Tricorder onto something (a biome, an ore, a
structure, a saved place), the owner looking at that player on the map should see the
target and the line from the player to it. Owner decisions: the target goes away once
the player reaches it, and a map share link shows it too.

**How.**
- `mc_set_waypoint` (the only writer of a Tricorder waypoint, fast path included) also
  records the target in `app.mc_tricorder_targets` (migration `0232`), one row per player
  per world, keyed by the lowercased gamertag as the pack's own copy is; a clear deletes
  it. The pack's copy is a world dynamic property nothing outside the game can read, so
  this is the map's only source; targets set before this shipped don't show until Dave
  sets the next one.
- **Arrival.** The track drain checks each new position sample (`targets.arrived`): a
  sample taken after the target was set, within 7 blocks across (the Tricorder's own
  "You're here" is 3; samples are kept every 10 s after 4 blocks of movement, so the last
  one can trail where the player stopped) and, for a target with a depth like an ore,
  within 6 blocks of it vertically, deletes the row and sends the pack's own clear
  (`scriptevent jb:waypoint {"player", "clear": true}`), so the Tricorder points at spawn
  again.
- `/map/players` (owner and share) gives each player a `target` (`dim, x, y, z, label,
  set_at`, or null). RLS: owner-only, plus a SELECT for a share link pinned to the row's
  world (as 0230).
- **The map.** With a player selected on the live view, their target is drawn as two rings
  in their colour with Dave's label, and a dashed bearing line from where they stand when
  both are in the dimension shown. Their card adds *Tricorder · Diamonds · 330 blocks
  north-east · y -58*, or *· in the Nether* across a portal. Not drawn on the replay: the
  target is where it points now.

Not built: Dave has no mob or animal finder, so "point me to a cow" isn't a target yet;
and a target is a fixed block, so a moving one would show where it was found.

### M22 — Locate anything: mobs, players, and every dimension (owner, 2026-10-12; shipped 2026-10-12)

**Why.** With the Tricorder's target on the map (§M21), the owner wants Dave able to find
the nearest of anything and point the Tricorder at it: a mob by type, a player, a
structure, a biome, a block or an ore.

**What changed.**
- **Every dimension.** `mc_locate` takes a `dimension` (in game, the asker's own) and runs
  `execute in <dim> positioned … run locate …`, so Nether fortresses, bastions and End
  cities, and Nether biomes, are found the way Overworld ones were. A search with no x/z
  starts from the player's last position in that dimension (for the Nether, else their
  Overworld one divided by 8, as a portal maps it). The deep-scan key carries the
  dimension.
- **Mobs: `mc_find_mob`.** Only the game knows where mobs are, so the jbrain pack answers
  a new `scriptevent jb:find {"player", "type", "max"}`: it takes that online player's
  dimension, `getEntities({type, location, closest: 1, maxDistance})` (at most 256 blocks:
  mobs exist only in the chunks around players) and replies with one line in the shape
  the block finder already prints (`find found minecraft:cow at x= y= z=, N blocks from
  <player> (<dim>)`, or `find none` / `find refused`). It is a look around, not a deep
  scan, and costs none. In game it searches around the asker only. A mob moves: the
  answer, and a Tricorder pointed at it, is where it was then. Pack 1.5.0; it reaches a
  world when the server next starts.
- **Players.** `mc_players` with a gamertag already said where a player is; the fast path's
  "point me to <name>" now falls back to an online player in the asker's dimension when
  no saved place has that name, and points the Tricorder at where they are.
- **Fast path.** "nearest <thing>" now also knows Nether and End structures (searched only
  from their own dimension; from elsewhere the model explains), 32 biomes by their
  everyday names, and 42 mobs (singular and plural). Every answer is an existing
  signed-off line (`found_one`, `locked_on`); a search that finds nothing goes to the
  model.
- Prompt `agent-minecraft-dave-v17`; `mc_locate` sidecar v4; `mc_find_mob` v1.

Only checkable on the box: that `getEntities` with `closest` answers within the console's
3-second reply window on a busy server, and Nether biome ids with `locate biome`.

### M23 — Dave finds items: chest contents and the ground (owner, 2026-10-12)

**Why.** "Which chest has my diamonds?" and "where did my stuff go?" are the questions
players ask most that Dave couldn't answer: `mc_find_block` sees blocks, not what's inside
them. The story is canon on the lore page ("The Tricorder's short-range scan", 2026-10-12).

**Owner decisions (2026-10-12):**
1. **Any chest.** A player may search every container in the world: chests have no owner.
   A player's own inventory and ender chest are not searched.
2. **Cost.** Within 3 chunks (48 blocks) of the asker it's their Tricorder's short-range
   scan, **free** (in game they need their own Tricorder on them, which `/dave` already
   requires). Farther is a **deep scan** through the §M17 ledger: two a Minecraft day in
   game, unlimited in the app, a repeat from about the same place free.
3. **First wave:** container contents from the world's files, and a live look around the
   asker for items on the ground (a death's drops).
4. **Freshness.** Answers carry the files' age; Dave says "as of N minutes ago" from 2
   minutes on. Before reading, the sidecar asks the running world to write itself out.

**What's built.**
- **Sidecar** (`deploy/minecraft/`): `nbt.py`, a full little-endian NBT reader (all 12 tag
  types, depth and size caps, every length checked against the bytes left, back-to-back
  compounds). `containers.py`'s `ContainerIndex`, modelled on `BlockIndex` (same 60 s
  refresh by the db files' sizes and mtimes), reads every `BlockEntity` record (key tag 49,
  with or without the dimension int): chests (trapped too, which share the `Chest` id),
  barrels, shulker boxes, hoppers, dispensers, droppers, furnaces, blast furnaces, smokers,
  brewing stands, crafters and chiseled bookshelves by their `Items`; item frames (`Item`),
  decorated pots (`item`) and lecterns (`book`). A shulker box carried as an item is read
  one level down (`tag.Items`), and its contents count toward the container holding it.
  A double chest (`pairx`/`pairz`, each half naming the other) is one chest. A loot chest
  nobody opened (`LootTable`, no `Items`) is skipped and counted, an unreadable record
  skipped and counted, ender chests never read. A `CustomName` is kept.
- `GET /map/items?slot&dim&x&z&items&radius&limit&flush` — the nearest containers holding
  any of 1–8 items (limit ≤ 20; no `radius` = the whole dimension), each with its kind,
  x/y/z, distance, count per item, how many are in shulker boxes, and its name; the totals
  across every match in range; suggestions when no name is in any container; the index's
  own counts (`records`, `unreadable_records`, `unopened_loot`, `containers_indexed`);
  and `as_of` / `age_s` (the newest db file's mtime). `/map/find` carries `as_of` /
  `age_s` too, and its `chest` now also searches `trapped_chest`, `barrel` and every
  shulker box colour.
- **Flush.** With `flush=1` the sidecar runs the snapshot's `save hold` → `save query` →
  `save resume` dance with nothing copied (`Bds.flush`, under the console lock, resume
  always sent), at most every 30 s and waiting at most 10 s, only for the loaded world
  while it runs. After one, the index re-reads at once and the answer is current
  (`age_s` 0). It's safe for the reason snapshots are: hold pauses saving, resume always
  follows; the cost is one forced save, no worse than a hot snapshot. The result's
  `flush` says what happened (`flushed`, `recent`, `not running`, `failed: …`, `not
  asked`).
- **Pack** (1.7.0): `scriptevent jb:items {"player", "types", "max"}` answers with one
  console line per kind per block within 48 blocks of that online player (`items ground: 3
  minecraft:iron_ingot at x= y= z=, N blocks from <player> (<dim>)`, nearest first, up to
  10), or `items none` / `items refused`. Item entities via `getEntities({type:
  "minecraft:item"})` and their `minecraft:item` component's `itemStack`, stable
  `@minecraft/server` 2.0.0. Containers are NOT read live: `dimension.getBlocks` over a
  97×97×384 volume is millions of block checks in one tick, which risks the script
  watchdog on a busy server; the files, flushed first, are fresh enough.
- **`mc_find_item`** (both Dave tiers): `items` (1–8, spaces become underscores), `x`/`z`,
  `radius`, `limit`, `dimension`, and `gamertag` (the app only). In game, a radius of 48
  or less around the asker is free and also looks on the ground around them; wider,
  omitted, or centred more than 48 blocks away is a deep scan (an id in no container at
  all is refunded, as `mc_find_block`'s unexplored names are). In the app it's unlimited
  and files-only, unless a `gamertag` is named, when the ground around that online player
  is looked at too. Each hit is a line in `first_hit`'s shape (`- 12 diamond in a chest at
  x= y= z=, N blocks away`, `- 3 iron ingot on the ground at …`); whichever of the ground
  and the containers holds the nearer hit is printed first, so the Tricorder is aimed at
  the best one. Player-written container names are listed after every hit, fenced. Prompt
  `agent-minecraft-dave-v18`, guide v9 (the Tricorder entry), `mc_find_item` v1.
- **Debug:** `GET /minecraft/map/items` passes through (with `flush`), for the box check.
- No new tables.

**Not built.** The fast path ("which chest has my diamonds") — no signed-off quick-answer
line for it on the lore page yet; once one is, it's a `_nearest`-style branch. Minecart
chests and chest boats are entities, not block entities, so they aren't searched.

**Box checks (after deploy).**
- The format on the owner's real imported world: `GET /minecraft/map/items?x=0&z=0&items=
  diamond` → `records` (how many key-49 records), `unreadable_records` (should be 0),
  `unopened_loot`, `containers_indexed`, and a known chest's contents matching the game.
  Then the Nether (`dim=nether`).
- Time the first (cold) call and a `flush=true` call: the cold read decompresses every
  table, as the block index's first search does.
- That `save hold`/`save query` answers within 10 s on the busy server (the `flush` field),
  and that `jb:items` answers within the console's 3-second window.

### M24 — Goals that count: "I need 23 iron blocks" (owner, 2026-10-12)

**Why.** A player says "Dave, remind me I need 23 iron blocks", and later "how many iron
blocks have I got, how many more do I need?". Dave should answer "17 of 23, 6 to go: 12
in chests near your base, 5 on you". Goals were titles only, and `mc_find_item` searches
any chest in the world, not "my stock".

**Owner decisions (2026-10-12):**
1. **A goal can count an item.** It has an optional item id and a count (1–100,000). Goals
   written before this read exactly as they did.
2. **"Mine" means two things.** It is what the player carries (inventory, armor,
   off-hand) plus the containers within 48 blocks of each of **their own saved places**
   in the loaded world, and within 48 blocks of where they stand. A container in reach
   of two places counts once.
3. **Cost.** Carried items are always free. Chests near a saved place are free only when
   that place is within the Tricorder's reach (48 blocks) of where the asker stands. If
   any counted place is farther, the check is **one** deep scan (§M17), not one per
   place, and the same check again from about the same place is free. If they're out of
   scans, Dave still gives the free part, and says the far places weren't counted
   because the deep scan needs to recharge. The app is unlimited. A whole-world count is
   still `mc_find_item` and costs a deep scan in game, as before.
4. **Own pockets only.** In game it is always the asker's. In the app it is counted only
   when the goal's own player is named and online.
5. **Reached means asked, not done.** When the count is reached, Dave says so and offers
   to mark the goal done. He marks it only if the player says yes.

**What's built.**
- **Migration 0237** (renumbered from 0236 after a clash with the panel alarms) adds nullable `item` and `item_count` columns to `app.mc_goals`. The
  item's CHECK is `mc_find_item`'s id pattern, the count's is 1–100,000, and a third
  CHECK makes them a pair (both or neither). There is no new table, and 0226's owner-only
  policy is unchanged. The integration isolation test now writes both columns as a
  non-owner and is refused.
- **Pack 1.9.0**: `scriptevent jb:carry {"player", "types"}` adds up that online player's
  inventory, armor and off-hand for 1–8 item ids. It answers one console line: `carry
  counted for <player> in <dim> at x= y= z=: minecraft:iron_block=5; shulker boxes=1`, or
  `carry refused`. The position lets the app count the chests around them too. **Carried
  shulker boxes are not read.** Stable `@minecraft/server` 2.0.0 has no way to see inside
  a shulker box item (the item-inventory component is beta-only). So the line counts the
  boxes, and Dave says what's inside them wasn't counted.
- **Sidecar** `GET /map/stock?slot&dim&centres=x,z;x,z&items&radius&flush` (radius 48 by
  default, at most 256, 1–101 points). It sums the item across every container within
  `radius` of any point. Each container is counted once, toward its nearest point (the
  first named on a tie), and the sum is broken down per point. It returns `as_of` /
  `age_s` and flushes like `/map/items`. Built on `ContainerIndex.stock`. One call per
  dimension, so a Nether base's chests are counted in the Nether.
- **`mc_goal_create` / `mc_goal_update` v2** take `item` (normalized as `mc_find_item`'s:
  spaces become underscores, `minecraft:` dropped) and `count`. Both are checked before
  any write. A create needs both or neither, and so does an update to a goal that counts
  nothing yet. `mc_goals` and the chat intro show "Get 23 iron blocks (iron_block ×23)".
- **`mc_goal_progress(goal?, gamertag?)`**, read-only, is in both Dave tiers. It is a
  separate tool rather than an `mc_goals` flag because `mc_goals` is a cheap database
  read, while this one asks the pack and the world files and can cost a deep scan. Each
  has its own failure modes, cost and step label. `goal` can be left out when exactly
  one open goal counts an item. In game, places in another dimension or more than 48
  blocks away are "far" and are paid for with `scans.charge`, which does no stored-answer
  replay: a repeat recounts live and free, because chests change. A failed count is
  refunded. The answer gives the headline ("17 of 23 — 6 to go"), where they are (fenced,
  because place names are player-written), which places' chests were counted, the files'
  age, the no-saved-places hint ("save your base"), the shulker-box caveat, the scan
  line, and the ask-before-done line.
- Prompt `agent-minecraft-dave-v20` (v19 on its own branch; v20 once merged with §M25): "remind me I need N X" makes a goal with an item and
  a count, the progress wording, and the deep-scan rule for far places.

**Goals in the app (owner, 2026-10-13; GUI gate `docs/mocks/minecraft-goals/`, variant B,
"expandable if clicked", look-only).** Built:
- **A goal knows its world.** `mc_goal_create` now writes `app.mc_goals.world`: the world
  folder places are filed under (the in-game asker's world, else the loaded one). No
  migration: the column was there since 0226 and was never filled. A goal with no world
  (made before this, or when the server couldn't say) reads as the loaded world's.
- **`GET /api/minecraft/goals`** (owner RLS): every player's goals and the newest ten
  journal entries of each goal, each goal with its world, the world's name and whether
  it is the loaded one. A database read; with the sidecar down it still answers.
- **`GET /api/minecraft/goals/progress?xuid=&flush=`** counts one player's open counted
  goals at once (`minecraft/goals.py`): one `jb:carry` for all their items and one
  `/map/stock` a dimension (eight items an ask), per world. It counts what Dave counts:
  the pockets (the goal's own player, online, in the loaded world), the chests near each
  saved place in the goal's world, and the chests near where they stand. It never calls
  `scans.charge`; a place beyond 48 blocks (or in another dimension) is counted and only
  marked `far`. A world that isn't loaded is counted from its saved files, without
  pockets. The screen counts when it shows the goals (no forced save); **Count again**
  passes `flush`, which saves the running world at most every 30 s.
- **The screen**: a glance line under each player with goals in the Players table, which
  opens all their goals in place; a goal opens its detail in place (sources, far, file
  age, the shulker caveat, the no-deep-scan line, its journal); the player Sheet gains
  the same Goals list and the latest journal. DESIGN.md "Players' goals" has the pattern.

**Not built.**
- **Dave's in-game count still uses the loaded world's places** for every goal, even one
  made in another world (the app counts each goal in its own world).
- **No fast path.** There is no signed-off fixed line for progress on the lore page.
- **Ender chests** are never counted, and neither are minecart chests or chest boats
  (§M23).
- **No way to clear a goal's item** once it is set. To stop counting, make a new goal.

### M25 — The Tricorder's live scan (owner, 2026-10-12)

**Why.** "Dave, set my Tricorder to scan for diamonds": the Tricorder enters a scan mode
and its arrow leads to the nearest such block around the player, re-aimed as they move
and dig, with no deep scan spent. Chests and containers stay on `mc_find_item`'s free
48-block look (§M23); this is the live 16-block sense. No other cost rule changed.

**Owner decisions (2026-10-12):**
- **Every 10 seconds** the Tricorder finds the **nearest matching block within 16 blocks**
  (one chunk) of the player, and the held Tricorder's arrow and distance point at it, the
  same line as a waypoint's. An ore id brings its deepslate twin (and the reverse), as
  `mc_find_block` does.
- **Any block**, not just ores; an id the game doesn't know is refused with a reply Dave
  can relay.
- **None within 16:** no arrow; the held Tricorder reads `Scanning: diamond · none
  nearby` (the block in plain words). The arrow returns at the next scan that finds one.
- **It ends only** when the player asks Dave to stop ("stop scanning"), when the arrow
  goes back to the waypoint they had before (or spawn), or when they ask for a new
  waypoint, which replaces it. Never on its own: it's kept per player in a world dynamic
  property, like a waypoint, so it lasts across sessions and rejoins.
- **Free**: the Tricorder's own short-range sense, never a deep scan.

**What's built.**
- **Pack** (1.9.0, `jbrain-pack/scripts/scan.js`, pure rules in `rules.js`): `scriptevent
  jb:scan {"player", "block"}` starts a scan, `{"player", "stop": true}` ends it, answered
  `scan set for <player>: <label> (<ids>)`, `scan stopped for <player>` (`(none was on)`),
  or `scan refused: …`. A new event rather than a `jb:waypoint` field: the scan has its own
  state, reply and refusals, and `jb:waypoint` keeps its message shape. The block is checked
  with `BlockTypes.get`; a bare ore name with no such block ("diamond") is taken as the ore
  (`diamond_ore`). The twins are the ore, `deepslate_`, `lit_` and `lit_deepslate_` forms,
  kept only where the game knows the id (redstone's lit ore comes along; nether gold gets
  no made-up deepslate twin). Stored as `jb:scan:<gamertag>`; the waypoint under it is
  never touched, so a stop simply drops the scan.
- **The scan.** `dimension.getBlocks(new BlockVolume(…), { includeTypes }, true)` over a
  33×33×33 box around the player (y clamped to the dimension), nearest by 3D distance to
  the block's centre. One call, not sliced with `system.runJob`: it's 35,937 blocks with
  the type filter applied natively, and only the matches reach the script. The first
  scan after a start, and any taking 10 ms or more, prints `scan took N ms for <player>
  (<label>)` on the console, so the real cost shows in the server log.
- **Cadence and staggering.** The arrow loop (every 5 ticks) runs the scheduler first:
  at most **one scan a frame**, for the most overdue scanning player, who is next due 40
  frames (200 ticks) later. Players who start together drift onto frames of their own
  and stay there, so no two scans share a tick (40 players fit in the 10 s at full
  cadence). A rejoin or a restart scans at once.
- **Between scans** the arrow shows the last block found. Each frame it re-checks that
  block with one `getBlock`: mined (or unloaded), the arrow drops at once and the scan
  runs again within a second (4 frames, no sooner, so digging through a vein doesn't scan
  every frame); walked more than 16 blocks from, it's hidden until the next scan.
- **Ending.** A `jb:waypoint` set ends a scan (`…; scan ended` on its reply). A clear ends
  it only with `"end_scan": true`, which Dave's `mc_set_waypoint` clear sends ("clear my
  arrow" is a new heading: spawn); the track drain's arrival clear (§M21) doesn't, so
  reaching an old waypoint never ends a scan on its own.
- **`mc_tricorder_scan(block | stop, gamertag?)`** (both Dave tiers): a tool of its own,
  not `mc_set_waypoint` fields, because the model reaches for it by its ask ("scan for",
  "stop scanning") and its answers (scanning, none was on, no such block) aren't a
  waypoint's. Whose Tricorder follows `mc_set_waypoint`'s precedent: in game only the
  asker's; in the app any named gamertag, online or not (the pack keeps it for them), or
  this chat's player. `mc_set_waypoint` v4 says when it ended a scan. Prompt
  `agent-minecraft-dave-v20` (with §M24), guide v11 (one Tricorder line), `mc_tricorder_scan` v1.
- **Fast path.** No new line: "stop scanning" goes to the model. The one signed-off line
  near it, "Arrow cleared. Ask me when you need a heading.", would be false after a stop
  (the arrow returns to their waypoint, not cleared). A new quick-answer line needs the
  owner's sign-off on the lore page first.
- **The map (§M21).** Built 2026-10-13 from the GUI gate `docs/mocks/minecraft-scan-on-map/`:
  the owner picked **variant A with C's depth line**, and its review's blocking findings
  are fixed (the plate keeps the name, the distance is the Tricorder's, Dave can read a
  scan). The M26 pipe, end to end:
  - **Pack** (1.13.0): `{"ev":"scan","op":"set|found|none|stop",player,label,ids}` (a
    `found` adds `dim,x,y,z`) when a scan starts, when what it finds changes, and when it
    stops (a stop, a waypoint set, Dave's clear). Sent from the 10 s scan step, so a player
    not holding the Tricorder is covered; a once-a-second check of every scanner's found
    block (one `getBlock` each) notices a mined target held or not, and the rescan it
    triggers sends the change within two seconds. A steady result is **resent once a
    minute** (every sixth scan): the card says *Found 40 s ago* from the last line heard,
    so it stays true (to the minute) without a line per scan. A failed scan (mid-portal)
    sends nothing, so the map keeps the last result rather than flickering to *none*.
    After each load, one `{"ev":"scans","all":[{player,label,ids}]}` from the
    `jb:scan:<gamertag>` properties (which now store the player's name), **retried each
    frame until it goes out**: the once-flag is set only after a successful emit (§M26's
    gate-snapshot lesson).
  - **Sidecar** (`bds.py` `SCAN_EVENTS`): `op` from its list, the dimension from the enum,
    coordinates clamped, the player cut to 32, the label to 40, at most 8 ids shaped like
    block ids, a `found` only with a whole block, a snapshot to 256 with `truncated`.
  - **Backend:** migration `0244` adds `app.mc_tricorder_scans`, a sibling of the targets
    table, never columns on it, so the waypoint under a scan is never overwritten: one row
    per world and casefolded gamertag, `state` `set` / `found` / `none`, the block,
    `scanned_at` (the last line heard) and `set_at`. Owner RLS plus the targets' share
    read (a share link reads its own world's), with its isolation test.
    `minecraft/livescan.py` applies the lines in the session drain: a snapshot replaces a
    world's set (a scan still on keeps its last result until its first new scan; a
    changed one starts empty), and `world_replaced` (reset, import) clears it.
    `/map/players` gives each player `scan: {label, state, found, at, set_at}`.
  - **Dave:** `mc_tricorder_scan` v2 gains `status` (read-only; in game only the asker's
    own): what it scans for, the reach (16 across, and the y range 16 up and down from
    where they stand, cut at the world's floor and ceiling), and what the last scan found
    with the held Tricorder's distance (across, to the block's centre), the compass word
    and how far below or above them it is; offline, that it picks up again when they're
    back on. `mc_show_map` v3 gains the focus `scan` (the gamertag in `label`): the card is
    centred on the scanner and draws the scan live. Prompt `agent-minecraft-dave-v23` (merged with §M4e's v22).
  - **The map** (DESIGN.md "The Tricorder's live scan on the Minecraft map"): every online
    scanner's 33 × 33 square (viewfinder corners and a hairline in their colour, a 10 %
    wash while something's found), the found block in §M21's target grammar, the bearing
    only for the tapped player, and the waypoint under a scan hidden. One plate,
    *BlockyFox · Scanning: diamond* (*· none nearby*), in the name's place, falling back
    to the name. The card: *Tricorder · Scanning: diamond · 13 blocks north-east · y -58*,
    *↓ 6 blocks below them · y -58*, *Within 16 blocks, y -64 to -36. Found 40 s ago.*
    Offline, only *Scanning: diamond — resumes when BlockyFox is back on*. Not on the
    replay; shared maps show it by the Tricorder target's rule.
- **Lore.** Canon on the lore page as "The Tricorder's live scan" (2026-10-12): "until
  you ask Dave to stop or ask him for a new heading", which is why Dave's clear (spawn,
  a new heading) ends a scan too.

**Box checks (after deploy).**
- The map's scan: that `scan` lines reach the drain on the real server (the debug API's
  `GET /minecraft/logs?tail=` shows the `[jbrain] {"ev":"scan",…}` lines), that the
  after-load `scans` snapshot lands once the properties are readable, that a mined block
  shows on the map within a couple of seconds with the Tricorder put away, and that a
  steady target's *Found N s ago* never reads more than about a minute.
- The cost of `getBlocks` on the real server: the `scan took N ms` lines in the server log
  (the debug API's `GET /minecraft/logs?tail=`) for a dense block (stone) and a rare one
  (diamond ore), with several players online. If a scan is regularly past a few ms,
  slice the box across frames with `system.runJob`.
- That the `Scanning: diamond · none nearby` line and the `·` separators render on Xbox and
  Windows like the waypoint line does, and the arrow re-aims as the player turns.
- That `BlockTypes.get` knows the ids players ask for (`diamond_ore`, `ancient_debris`,
  `lit_redstone_ore`) and refuses a made-up one, on 1.26.

### M26 — Beacons and gates on the map (owner, 2026-10-12)

**Why.** §M12 and §M16 both said their gates and protected areas "show on the M8 map", and
the map's Layers tab listed *Wormhole gates* with a *later* badge. The owner wants to see
at a glance *what's protected* and *where a gate goes*, and to ask Dave "where's my gate,
where does it lead, is my base protected?". GUI gate: `docs/mocks/minecraft-map-markers/`;
the owner picked **variant A, as revised** (always drawn), the independent review's pick.

**The data path** (the mock README's "Data path", built as recommended):
- **Pack** (1.10.0): beside every prose line (which Dave's tools still read), a structured
  one. `{"ev":"beacon","op":"on|off",dim,x,y,z,by}` — the beacon now records its placer
  (`by`, from `playerPlaceBlock`; on `off` it's who broke it, empty when the sweep found it
  gone). `{"ev":"gate","op":"planted|opened|aimed|dormant|collapsed",id,dim,x,y,z,axis,
  planter,by,target}` — `target` is `{place,dim,x,y,z}` or null. After the first tick of
  every load, one `{"ev":"beacons","all":[…]}` and one `{"ev":"gates","all":[…]}`
  snapshot, so a restore, an import or a missed line can't leave the map stale. The
  structured line goes first, so a test's or a reader's "last line" is still the prose;
  `_reply` drops event lines from a console reply. Dimensions are the short names.
- **Sidecar** (`bds.py` `MARKER_EVENTS`): the four kinds join `PACK_EVENTS`, every field
  type-checked like deaths: ints clamped to ±30,000,000, the dimension and `op` from closed
  lists, the axis `x|z`, names and places cut to 40, a snapshot to 256 records with
  `truncated` set when cut. They carry the world folder and no xuid.
- **Backend:** migration `0240` (renumbered twice, after clashes with the chat share links and the agent session origin) adds `app.mc_world_beacons` (keyed by world and block) and
  `app.mc_world_gates` (keyed by world and the pack's gate id), owner-only RLS with
  isolation tests. The session drain hands each event to `minecraft/markers.py`: an event
  patches, a whole snapshot replaces a world's set (a cut one only patches), and
  `world_replaced` (reset, import) clears them. A snapshot keeps the first-heard
  `placed_at` / `planted_at`; a gate whose target moved unheard keeps no `aimed_at`.
  `GET /api/minecraft/map/markers?slot=&dim=` → `{beacons, gates, as_of}` answers with the
  server stopped or the world unloaded. Debug: `GET /minecraft/markers` (counts only).
- **Dave:** `mc_gates_beacons` (read-only, both tiers, sidecar v1): where each gate is (the
  middle of its ring), open or dormant and where it leads (with the distance and compass
  from the gate, or the dimension), each beacon and its square, and which of the player's
  saved places a beacon covers (a place's chunk within one chunk of a beacon's, same
  dimension). In game it's only the asker's own, and the item guide (§M19) gates it: no
  gates before `wormhole_seed`, no beacons before `no_spawn_beacon`, and the door offers the
  tool only with one of them. In the app it's everyone's, or one `player`'s. `mc_show_map`
  (v2) gains the focus kinds `gate` and `beacon` and the overlays `beacons` and `gates`
  (on by default). Prompt `agent-minecraft-dave-v21`.

**The map** (`minecraftMapMarkers.ts` for the rules, `minecraftMapDraw.ts` for the
drawing, shared by the full map and the chat card):
- A beacon is a shield on a green-ringed halo tile, its 48 × 48 square a green dash over a
  halo, the tapped one filled. A gate is the portal on a violet-ringed tile, filled while
  open; dormant, the same portal unfilled in a dashed ring. An open gate draws a dotted
  violet line (1 on, 6 off) with an arrowhead to a violet-ringed pin labelled with the
  place; into another dimension it gets a label (*to Nether base · in the Nether*) instead.
- **Draw order:** squares, lines, pins, tiles, the selection ring, then **players on top**.
- **Labels:** a gate's label goes right, left, below or above its marker, else is dropped;
  every label now keeps off the beacon and gate tiles, the pins, the player discs and every
  gate line (and the Tricorder bearing) but its own.
- **Layers:** **No-Spawn Beacons** and **Wormhole gates** switches (no *later*), the
  **In this world** list (every beacon and gate in any dimension; a tap selects it and
  flies there), and the legend. **Tap** a gate: *open / dormant*, whose, planted and aimed,
  *Leads to Tree farm · 244 blocks north-west · X −160 · Z −140* with **Show Tree farm**
  (both ends framed) or **Show it in the Nether**; a dormant one says how to open it. Tap a
  beacon: *protecting*, who placed it, the square to the block and its overlaps.
- **Chat card:** chips **Beacons · Gates**; a `gate` or `beacon` focus is found in the box's
  own record and drawn selected (line, pin and label).
- Hidden on the timeline's replay (they are where the pack last reported them). The share
  map has none: the rows are owner-only.

**Only checkable on the box:** that `playerPlaceBlock` carries `player` for the beacon (it
does on the stable API, but an older beacon has no placer and shows *placer not
recorded*); that the after-load snapshot lands once the world's properties are readable
(first tick of the interval) on a world with many gates; and that a world restored from a
backup is re-reported by its next start.

### SW1–SW4 — Spawn wreck: a crashed escape pod under a damaged mothership (owner, 2026-10-11)

**What the owner asked for:** every **new** world starts at a crash site. An escape pod
lies in a small crater at spawn, its parachute dragged out behind it. Overhead hangs the
battle-damaged carrier it came from: intact enough to fly, with breaches showing lit decks,
smoke, and furnished interiors reachable by ladder. The builds were designed and
signed off from renders in `spikes/minecraft-spawn-wreck/` (its README describes both).

**How it gets into a world.** Bedrock has no hook into terrain generation, so the build is
done by a script on the world's first load:

- **Structures.** The spike's generators export the pod's hull (with its interior air,
  so terrain inside it is cleared) and the mothership (tiled at 64 × 64) as block lists.
  `deploy/minecraft/structures.py` (stdlib) turns them into `.mcstructure` files at
  image build. Block states are checked against Mojang's block metadata, because a
  wrong state set breaks a block silently.
- **A separate `jbrain-wreck` behavior pack**, installed only into an *armed* world.
  - **Arming:** a slot is armed when the wrapper creates or regenerates it (create,
    reset with a seed) and the `spawn_wreck` setting is on (default on). A create may
    override that. An imported or restored world is never armed, so the owner's own
    worlds are never touched.
  - **First start:** a brand-new world has no folder until BDS generates it, so the
    wrapper writes the pack into the empty folder before the first start.
  - **Fallback:** if the pack doesn't report itself loaded on that first start, the
    wrapper restarts once to install it.
- **Once per world.** The script records progress in world dynamic properties and
  emits `wreck` events (`loaded`, `pod`, `ship`, `done`). The in-world record is
  the guard against a second build. The wrapper marking the slot `done` (which takes
  the pack out at the next start) is housekeeping, because a `done` can go unread. A
  build interrupted mid-way resumes; a finished one never repeats.

**The pod adapts to the ground it lands on.** Only the hull is a stored structure. The
script builds the rest column by column:

1. **Load and survey.** It waits for the spawn chunks to load (a temporary ticking
   area), then surveys around spawn. It looks through leaves, logs and plants to the
   real ground, rejects water and steep ground, and picks the flattest dry patch near
   spawn.
2. **Fit the ground.** It fills under the hull where the ground drops away and trims
   where it rises. It clears trees and snow from the footprint and skid path.
3. **Build the site.** It digs the crater and skid mark in scorched versions of the
   local ground, chosen by the surface block:
   - **grass:** coarse dirt
   - **sand:** sandstone and gravel
   - **red sand:** red sandstone
   - **stone:** cobble and gravel
   - **snow:** dirt with the snow cleared
   - **mud:** packed mud

   It drapes the parachute at each column's own height and lays the blown hatch cover.
4. **Fill and point spawn.** It stocks the barrels with a small starter kit and moves
   the world spawn to the hatch, with `spawnradius` set to 0.
5. **Splashdown.** With no dry ground within reach, the pod floats with its canopy on
   the water.

**The mothership** is placed from its tiles above spawn. It first checks the highest
terrain under its footprint and lifts itself clear of peaks, keeping its top under the
build limit; if it can't lift enough, it shifts sideways. Fires are kept clear of
flammable blocks, and well away from the magazine's TNT.

**SW1 — pipeline + arming (no in-game change yet; shipped):** a world armed under SW1 is
passed over. Its pack records that in the world and reports done, so SW2's build never
lands on a world that was played before it shipped.
- the export
- `structures.py` with its NBT writer and tiling
- block-state validation
- the image build step
- the `jbrain-wreck` pack skeleton, which only reports `loaded`
- slot arming, `spawn_wreck`, pre-generation install and the `wreck` event

**SW2 — the build (shipped):**
- **Pod stage.** It loads and reads a 128-block square round spawn: one ticking area,
  under the 100-chunk cap, because with nobody online only ticking-area chunks load. It
  chooses the site and records it, with the ground it read, in the world before
  changing anything. Then it runs its steps in order, each recorded as done:
  1. terrain
  2. place
  3. crater
  4. canopy
  5. details (the hatch cover, a level apron at the hatch and two campfires)
  6. kit (only into empty barrels)
  7. spawn

  A resumed build continues at its step, on the first-read heights, so it never digs
  or places twice.
- **Ship stage.** It reads the peaks under each candidate spot and keeps a ship that
  started too high under the build limit. The chosen spot, and each tile placed, is
  recorded.
- **Failure.** It waits for every chunk of a box before reading it, because an area
  loads over several ticks. A failure is reported (`failed`) and retried 30 s later in
  the same session, before anyone has built at spawn. It is given up after three
  tries. Ticking areas come out on every exit.
- **Wrapper.** If a brand-new world's first start shows no sign of the pack, the
  wrapper restarts it once, as a plain restart under the lifecycle lock (never an
  update) and only while nobody is on.
- **Tests.**
  - `plan.js` is pure and node-tested.
  - `deploy/minecraft/wreck-sim/` runs the real script against a fake
    `@minecraft/server` that is as strict as the game about unloaded chunks. It covers
    plains, forest, jungle, desert, snowy, mountainside, badlands, swamp, mangrove,
    island, open ocean, a high plateau, peaks under the ship, a placement refused once,
    and a server stopped dead at 1%, 30%, 60% and 90% of a build. The fake's ticking
    areas load chunk by chunk.
  - Every one must come out right, and a resumed build must equal an uninterrupted one
    block for block.
  - The supervisor's pytest runs both suites.

**SW3 — operate it from the PWA ✅.** It closed the rule #10 gap: the default-on
`spawn_wreck` setting had no switch. The GUI gate's three mocks are in
`docs/mocks/minecraft-wreck/`, and the owner chose **A, a switch in the sheet**
(`DESIGN.md` "Crash site at spawn"):
- **New world and Reset.** These sheets carry a **Crash site at spawn** switch, which
  starts at the default. A reset takes a `wreck` override, as create does. *Empty*
  carries no switch.
- **Server settings.** It carries **Crash site by default**, which saves at once with no
  restart. Turning it off leaves a world already armed as it is, and the toast names that
  world.
- **Each world says where its crash site stands,** on its row and on its page: *on its
  way*, *building* (with its phase), *didn't finish — trying again*, *built*, *not built*
  or *off*. The sidecar keeps each world's latest build phase (`wreck_phase`) and ignores
  a phase from before the world was made. A pack `done` that carries a cause is recorded
  as **`skipped`**, not `done`.
- **There is no re-arm action.** A played world can't get a crash site, because it
  would land on what's built there. A reset with a seed is the way back.
- **No new debug route.** The debug API's `GET /minecraft/worlds` and `GET /minecraft`
  already return the new fields.

**SW4 — prove it in every biome, on the box.** It needs the owner's debug token, and a
debug-API way to make test worlds. The owner chose to move the boundary (2026-10-11): the
token can make **test worlds**, and the owner's worlds stay read-only to it.
- **The route ✅.** `POST /debug/minecraft/test-worlds/{slot}` makes a world from a seed,
  only in an empty slot, and marks it `test`. `…/test-worlds/{slot}/reset` regenerates or
  empties only a `test` world. `POST /debug/minecraft/worlds/{slot}/load` loads any slot
  holding a world, and is refused while anyone is on, so a test never puts the family out
  of the game. The owner renaming, changing, resetting, importing or restoring over a test
  world clears its mark, so the token can't touch it again; the sidecar refuses to make
  or reset an owner world as a test one, behind the API's checks. The PWA shows a test world as *test world —
  made over the debug API*. `DEBUG_ACCESS.md` has the routes.
- **The sweep ✅** (2026-10-11; results below). Worlds are made from
  seeds whose spawn `mapping.Predictor` puts in each biome, loaded back to back (each load
  backs up the world it leaves), and the owner's world is loaded back once, at the end. Each one is checked from the world index:
- the pod is present;
- it is neither floating nor buried;
- its hatch is reachable;
- its canopy is on the ground.

**Box-only checks** (can't be proven off the box):
- BDS honours a pack pre-written into an ungenerated world's folder. **Confirmed
  2026-10-11:** the first test world loaded both packs and the build reported in.
- Every palette entry places as intended on the running version. **Confirmed
  2026-10-11:** every pod step's writes read back as written in all 15 biomes, and the
  hull and mothership samples matched.
- Placing a whole build in one go stays inside the script watchdog. **Confirmed
  2026-10-11:** each world built in about 16 s with no watchdog warning.

**What the box taught (SW4 sweep, 2026-10-11).**
- The first test world gave up in about a minute: *N chunks … never loaded*, three
  times over. `loadBox` counted its 90-second wait in yields, but the game runs many job
  steps a tick, so a new world's spawn chunks got under a second to generate. The wait
  is now in game ticks (`system.currentTick`).
- The simulator now runs 50 job steps a tick and loads chunks by the tick, so the old
  code fails there too.
- With that fixed, the pod, its supports and the mothership came down whole in plains,
  forest, jungle, desert and snowy plains, in about 16 s each. But **none of the
  script's own block edits landed**: no tree clearing, crater, parachute or hatch
  apron. In the jungle the hatch opens onto a grass block. The structure placements
  stuck, and an edit from the console in the same place stuck too. The pack logged
  nothing, because its warnings went to `console.warn`, which the box's console showed
  none of. Warnings now go out as log lines. Each pod step logs how many of its block
  writes landed, and the first reason one didn't. The simulator fails a build whose
  writes don't land, or whose crater, parachute or details write nothing.
- The next run named it: every failed edit was *no block there (chunk not loaded?)*.
  The pack set its ticking area up while planning, then removed and re-added it to
  build. On the box a removed area's chunks unload a tick later, so the load check
  passed on chunks about to go. The structures were already down, but the edits fell
  on nothing. Now an area the run already has is kept. After any real removal, the old
  chunks are waited out (2 s) before the load check. In the simulator, a removed area's
  chunks now last one tick, and a new area has none ready on the tick it's added, so
  the old code fails there exactly as it did on the box.
- That fix made each build take about 10 minutes before the pod was placed. Its 2-second
  unload wait ran while no ticking area was up, and with nobody on the world all but
  stops ticking then: 13 ticks in its first 2.5 minutes. Now nothing waits without an
  area up. A replaced area is removed and re-added in the same tick. `loadBox` catches
  the late unload by reading every chunk again each tick, until the box has held for 5
  ticks. A build that will be retried keeps its areas, so its 30-second retry doesn't
  take hours either. In the simulator, removing a missing area no longer throws, which
  matches the box, and a build fails if it spends a tick with no area up.
- With that, the pod builds in about 49 s after load, every block written. The mothership
  then stalled for over 14 minutes. **With nobody on, the box's world ticks about 0.3
  times a second even with a ticking area up** (337 ticks in 15 minutes), so the
  steady-load check and its 90-second give-up, both counted in ticks, could take hours.
  The waits are now on the clock: `loadBox` gives up after 90 s, and "held" means every
  chunk read fine on 3 ticks at least 1 s apart. A slow load logs how many chunks it's
  still missing every 15 s, so the next run says what's stuck. The simulator's clock
  runs with its ticks, and a new `slow_and_stuck` scenario, a tick every 3 s and a spawn
  chunk that never loads, must give up within its waits.
- The clock waits still gave up, in jungle and in plains. The pod and the mothership each
  needed a retry, and the mothership never finished: 19 of its 84 chunks (6 in plains)
  stayed missing for minutes. The progress lines showed the cause, and it wasn't the
  empty world. During each 30-second retry, when the build job wasn't running, the world
  ran 600 ticks a minute. While `loadBox` waited it ran about 1 tick every 15 s. The wait
  yielded in a tight loop, and a job that never stops yielding gets the rest of every
  tick, so the server starved itself and its chunks barely loaded. The 0.3 ticks a second
  above, and the "idle" world before it, were this same spin. Now `loadBox` hands the job
  back between reads (`system.runTimeout`, one tick) and picks it up on the next tick, so
  waiting costs the world nothing. In the simulator, every job step now costs wall time
  and `runTimeout` passes real ticks, so a spinning wait runs out its 90 s before its
  chunks load: the old pack fails there, as on the box.
- With the spin gone the world ran a full 20 ticks a second during waits, and the pod
  built in 4 s after load. Forest came down whole in 31 s. But in jungle and plains the
  mothership still had chunks that never loaded: 19 and 6 of its 84, and 25 of 90 in
  desert. Mapped from the console in plains, they were the area's whole far column. A console area over just
  those chunks loaded them at once, and so did removing and re-adding the same ship area
  from the console a few ticks apart; plains then finished whole. The pack had added the
  ship's area in the same tick it removed the pod's, and an area added then can come up
  short. Now an add waits out the tick of any removal, and the pod's area stays up until
  the build is done, so the world is never without one. A load that gains no chunk for
  15 s gets a patch area over the chunks still missing. In the simulator, an area added
  in a removal's tick never loads its last column and row, and a `short_ship_area`
  scenario, whose ship area always comes up short, must be patched and finish whole.
- With that, the full sweep ran in about 16 s a world, every one with no retry. In
  plains, forest, jungle, desert, snowy plains, badlands, savanna, taiga, meadow,
  dark forest, mushroom, ocean and deep ocean, the hull (16/16), its supports, the open
  and walkable hatch, the dry interior and the mothership (8/8) all checked out, and
  every pod step wrote every block. (Several seeds spawn in a neighbouring biome: dark
  forest's in a pale garden, mushroom's in old-growth taiga, ocean's in plains and deep
  ocean's in a forest.) Two biomes taught something:
  - **Swamp:** the hatch opened onto water, and 5 of the 20 interior samples were water.
    The footprint was dry ground two blocks below the surface of the pools beside it,
    and they poured in through the hatch the moment it was opened. Now a dry site is
    raised to the highest water surface within 12 blocks of it, and the terrain step
    and hatch apron build the ground up to the deck. The simulator's `low_swamp`
    scenario, dry ground below the pools beside it, fails the old planner on a new
    `hatch_dry` check: no water at the hatch's height within 16 blocks.
  - **Mangrove swamp:** the pod came down whole, but 59 of the terrain step's writes
    read back as water. A waterlogged mangrove root keeps its water when it's set to
    air. Such plants are now cleared to water, and the ground read counts the water
    they stand in, so the site is raised above it too. In the simulator, mangrove roots
    stand in the shallows and keep their water when cleared, as on the box.
  - **Deep ocean (on land):** one of the 12 support samples had air under it, a natural
    cave pocket under the pod's edge (air from 65 to 68 under a dirt crust). The
    terrain step fills up from the ground it read, so a hollow below that stays. One
    column, in a pod otherwise whole and walkable, is left as it is for now.
- **Swamp and mangrove swamp, again,** after the water fixes, with plains and jungle as
  controls: all four came down whole in 16 s, with the hatch dry, open and walkable, the
  interior air, the supports 12/12, the mothership 8/8, and every write landed. Swamp's
  terrain step now writes 735 blocks to build the site up above its pools. Every biome
  in the sweep has now passed. The owner's world was loaded back, and the test slot was
  emptied.

### DW1 — Dave's welcome and the crew's Tricorder (owner, 2026-10-11; built 2026-10-12)

**What and why.** The story is canon in Bleepbloop_City's
[`notes/lore.md`](https://github.com/jeffmhopkins/Bleepbloop_City/blob/main/notes/lore.md#dave-the-crew-and-the-tricorder).
That page is the source of truth for the story and for Dave's exact words; this section
is only how jbrain2 builds it. In short: Dave is the Old Makers' satellite AI in orbit, and
every player is crew who woke beside their crashed pod. Dave reaches them only through
their Tricorder, and death is his transporter. The owner signed off Dave's lines on the
lore page (2026-10-11), so DW1 can be built from them.

**Where it lives.**
- **The Tricorder** moves from the home-test kit into the always-installed `jbrain`
  pack, with its icon in a `jbrain` resource pack. Every world gets it, Bleepbloop City
  included.
- **It keeps M9's arrow:** held in hand, it points at the player's waypoint, or at spawn,
  which in a crash-site world is the pod's hatch.

**Rules, as the pack enforces them:**
- **Only its owner's.** The 5-tick arrow loop shows nothing for a Tricorder whose owner
  xuid isn't the holder's, just a static actionbar line.
- **No Tricorder, no Dave.** The `/jb:dave` handler looks for **the asker's own**
  Tricorder anywhere in their inventory before the question reaches the backend. That's
  the item's owner xuid, an item dynamic property, as M9 planned. Without one, a fixed
  static line answers, with no AI turn. Someone else's Tricorder gets the "not tuned to
  you" line.
- **Bound to the player (canon, owner 2026-10-11):** created with
  `ItemStack.lockMode = ItemLockMode.inventory`, so it can't be dropped, thrown, put in a
  chest or moved out of the inventory, and `keepOnDeath = true`, so a death keeps it.
  Both are stable API. A box check confirms it in Bedrock's own UI on Windows and Xbox:
  the Q / drop key, dragging it out, a chest, a death.
- **Re-beamed on waking (a safety net):** on every `playerSpawn`, both a join and a
  respawn, a player without their own Tricorder gets a new one, with the "nearly lost
  you" line. Anything that slips past the lock lands here: `/clear`, a hopper-minecart
  edge, an old unlocked copy. A new one retires any other copy with the same owner,
  following M9's one-at-a-time rule.
- **Power Pack uses (M10)** need the player's own Tricorder on them too, above.

**First-time beats.** Each is once per player per world. A world dynamic property keyed
by xuid records which beats each player has had, so the pack needs no backend for them.
- **Waking** (first `playerSpawn` with `initialSpawn` in this world). The sequence, timed
  with `system.runTimeout`:
  1. Darkness;
  2. a "…where am I?" actionbar;
  3. particles falling onto the player, with sounds;
  4. "…what's this?";
  5. the Tricorder goes into their hand;
  6. the title;
  7. Dave's lines.
  - The lines are scripted, not an AI turn: instant, the same for everyone, and safe
    for kids. Only the gamertag fills in.
  - A crash-site world (the slot's `wreck` record, or the wreck pack's own record) swaps
    the two lines that point at the pod and ship.
- **First death** (`playerSpawn` after `entityDie`). **First Ender Pearl**, from an
  inventory check on the 5-tick loop M9 already runs.
- **First Power Pack** only once M10's uses ship. Chests (M11) and wormholes (M12) get
  their beats with those waves. A beat never describes what isn't built in that world.
- `/jb:dave hello` replays the welcome's Dave lines, without a second Tricorder.

**Dave's voice.** `minecraft_dave.prompt` gains a short story section copied from the
canon: who Dave is, the crew, the Tricorder, the transporter.
- **Light flavor:** answers and coordinates first, with at most one small in-character
  touch.
- **Lore** only from the canon, never invented.
- **No false claims:** never that the ship or the Tricorder can do something the tools
  can't.
- When the canon changes, the prompt is updated to match, in the same PR as any code it
  affects.

**Shipped (wave mc-items, 2026-10-11): the rules, not the welcome.** The Tricorder is in
the `jbrain` packs in every world (M9's note), and the pack enforces: owner only (static
for anyone else), no Tricorder no Dave (`/jb:dave` answers the static lines and sends
nothing to the backend), bound to the player (`lockMode`, `keepOnDeath`), and re-beamed
on every waking with the "nearly lost you" line, retiring older copies. A player's
first-ever waking in a world gets theirs quietly: `firstWaking()` in the pack's
`main.js` is the hook the welcome sequence replaces. Dave's lines are one constants
block, `LINES` in `jbrain-pack/scripts/rules.js`, copied verbatim from the lore page.
The welcome, the first-time beats and `/dave hello` followed on 2026-10-12 (below).

**Shipped (2026-10-12): the welcome and the first-time beats.** All in the pack, no
backend: `jbrain-pack/scripts/welcome.js` plays them and `welcome_rules.js` holds the
timings and choices (node-tested); the words are new entries in `LINES` (`rules.js`),
copied verbatim from the lore page. `main.js`'s quiet `firstWaking()` is gone.
- **The welcome** follows the lore table to the tick with `system.runTimeout`, private to
  the player: `camera.fade` to black for 2 s with "…where am I?"; end-rod sparkles falling
  in a column onto them (`Player.spawnParticle`) with a climbing `note.chime`; "…what's
  this?"; at 5 s a chime and their Tricorder beamed into the selected slot; a crackle and
  the **SIGNAL ACQUIRED** title; then Dave's six lines 3 s apart. Each step re-checks the
  player, so leaving mid-welcome ends it quietly.
- **Who gets it:** once per player per world, on a join with no `welcome` beat, which
  includes **players who joined before it shipped** (owner, 2026-10-12: they see it on
  their next join). One who already has their own Tricorder keeps it: no second copy, it
  is selected if it's in the hotbar. Beats are a JSON list per player in the world
  property `jb:beats:<id>`, beside M19's `jb:known:<id>`; `welcome` is recorded as the
  sequence starts, so a rejoin mid-welcome doesn't loop it (one who left before the beam
  gets their Tricorder quietly on the rejoin). The re-beam safety net still runs on later
  wakings and stands down during a welcome, which hands the Tricorder over itself.
- **Crash site or not** reaches the pack from the wrapper: `pack_config` adds `"wreck"`
  to the `scriptevent jb:config` line it sends at every load, true for a slot whose
  `wreck` is `armed` (building on this very load) or `done`. The pack keeps it as the
  world property `jb:wreck`; never told reads as no wreck. A wreck that passes its world
  over (`skipped`) sends the line again at once with `false`.
- **First death** (the first respawn, beat `first_death`): Dave's two lines; when a Power
  Pack saved their things on that death, only the first, so he never says they're where
  they fell. **First Ender Pearl** (`first_ender_pearl`): the first look with a pearl on
  them once M19's `power_pack` unlock is recorded. **First Power Pack** (`first_power_pack`): the field line only
  with the keep switch on, the saved-places line only with the transport switch on;
  neither on, nothing is said or recorded, so it comes the next time they hold a pack
  after the owner turns one on. The pearl and pack beats wait for their own Tricorder
  on them, like every other Dave line (a respawn always has it, or is re-beamed one): an
  unlock made without it leaves the beat unheard for a later look, never spent (#1684).
- **Beats never cut into Dave's speech** (#1683): while the welcome, a `/dave hello`, the
  first-death lines or the first-wormhole line (from the plant, through its 1.5 s wait)
  are playing, no other beat speaks, and a `/dave hello` asked then is dropped. The ones a look re-checks
  (pearl, pack, Replicator) stay unheard until a look after he's done; a death or a
  planted gate waits and plays once he's done. So a death mid-welcome gets its lines
  after the welcome, with the Tricorder it handed over, and a pre-DW1 player carrying
  packs hears their beats after it, not over the darkness. A player who leaves before a
  queued beat plays keeps it (in the script's memory, so not across a server restart): it
  plays once on their next join, after anything Dave says first (#1698).
- **`/dave hello`** (also `/d`, `/ai`; exactly "hello", any case) replays the six lines at
  the welcome's pace, with no darkness and no Tricorder, answered in the pack; no
  question reaches the backend. It still needs their own Tricorder. "hello there" is a
  real question and goes to Dave as before.
- **First wormhole** (owner, 2026-10-12, with the gate's lines signed off): just after a
  player's first planted line, Dave says "…a fold, held open. Stand close and tell me where
  it should lead." Beat `first_wormhole`, through their own Tricorder only; `wormhole.js`
  calls it through `initWormholes({ onPlanted })`.
- **First Replicator** (owner, 2026-10-12, with the panel signed off): when
  `replicator.js` sees 8 Power Packs held, Dave says "Eight Power Packs…
  that's enough to wake a Replicator. …". Beat `first_replicator`, own Tricorder only,
  through `initReplicator({ onUnlocked })`, which is called on every look while they hold
  8 (not only the unlocking one), so a player without their Tricorder hears it later.
- **Not here:** the chests' beat; it comes with M11 and lines on the lore page.

**Shipped (2026-10-12): Dave's story.** `minecraft_dave.prompt` (`agent-minecraft-dave-v16`)
carries a story section copied from the lore page's canon: Dave, the Old Makers, the
ship and the crew, death as the transporter, their ruins and the tie-ins (both promoted
to canon by the owner, 2026-10-12). Why the Old Makers vanished is sealed: Dave says so
in the canon's words and never guesses. The ores table and "three ways it knows the
world" stay proposals and are left out. It rides the persona, so the app and the game
both have it; the items stay gated by the guide (§M19). The guide (`v7`) now says a
Tricorder can't be lost and drops the "someone else's" static, which stays only as the
pack's safety net.

Every line the pack uses is signed off on the lore page (owner, 2026-10-11), including
"This tricorder is retired" (§M9) and "Your Power Pack burnt out…" (§M10).

**Tests.** The pack's beat bookkeeping and the Tricorder checks get node tests against
the fake server, as the wreck pack's do. The prompt change gets a prompt test.

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
| Goals and progress log, **per player** | `mc_goal_create` / `mc_goals` / `mc_goal_update` (done, abandoned, rename; an item and count since §M24), `mc_goal_progress` (§M24), `mc_log` (add a progress entry, optionally against a goal), `mc_log_read` (a player's journal, filterable by goal or date) | `app.mc_goals` and `app.mc_goal_log`; see "Goals and the progress log" below |
| Memory, **per player**, self-managed | `mc_memory_read`, plus **line-level edits only**: `mc_memory_add(text)`, `mc_memory_replace(line, text)`, `mc_memory_remove(line)`. There is no whole-document write. | `app.mc_player_memory` (one row per line, with history); see "Memory: self-managed, never overwritten" below |

**Goals and the progress log (owner, 2026-10-10).** The "session log" is a **journal of
progress toward a player's goals**, and Dave helps that player get there. Everything is
tied to a **Minecraft player**, keyed by xuid like the play history, so a gamertag change
doesn't lose it.

- **Goals** (`app.mc_goals`): player xuid, world folder (written since the app's goals,
  §M24; empty reads as the loaded world), title (for example "Beacon at the
  base" or "20 obsidian for the portal"), optional target notes, status
  (open / done / abandoned), created and finished times; and since §M24 an optional item
  and count ("iron_block ×23"), which `mc_goal_progress` counts against what the player
  carries and the chests near their saved places.
- **Progress log** (`app.mc_goal_log`): player xuid, optional goal, time, text, and
  **source**:
  - `owner`: written from the PWA agent;
  - `player`: typed in game, e.g. `/jb:dave log got 12 obsidian`;
  - `dave`: Dave's own summary entry, made only when the player asks;
  - `auto` (M5): add-on events: transport trips, and each death (2026-10-13), such as
    "Died at 12, 64, -31 in the Overworld (fell from a high place)"; crafting is still to
    come.
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
  - `mc_find_block` (owner, 2026-10-11: "can you point me to the nearest diamond"): the
    nearest blocks of any kind in **explored** ground, with exact x, y, z
    (`deploy/minecraft/blocks.py`). It reads each section's palette from its
    `SubChunkPrefix` record (tag 47; version 9, little-endian NBT palette, 4096 packed
    x,z,y indices), keeps only which block names each section holds — names, not
    blocks, so a big world's index stays small — and decodes positions only in the
    sections that hold a wanted block, nearest chunk first, reading just those records
    through each table's index (`leveldb.read_keys`). An ore name brings its deepslate
    twin. Checked on a real 1.26 world: the game's own `testforblock` confirmed every
    find it was asked about (two diamond ores, a spawner, a chest), and the block above
    a find reads as what it really is. Unexplored ground can't be searched — ore
    placement doesn't exist until the game generates the chunk.
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
- **P3 — maps ✅ (2026-10-11, viewer wave 3 in §M8).** M8's renderer, the `minecraft_map`
  tool, and the map tool-view (GUI gate), also embedded on the Minecraft screen. Built as
  the tool `mc_show_map` and the live `minecraft_map` card, not an image (the GUI gate's
  carve-out); the Minecraft screen's way in is its **Map** row (wave 1).

**Decided (owner, 2026-10-10):**
- **No approvals** for goals, log or memory writes, **provided the session has a defined
  player**. A PWA chat with no gamertag set refuses the writes and asks for one, rather
  than guessing.
- Minecraft_Dave runs on the **local model**.

**P2 progress (2026-10-11):** the in-game door (§M5 above) — the same persona, goal,
log and memory tools reach players in game at player tier, scoped to the asker's xuid —
plus saved places (`mc_place_save`, `mc_places`, `mc_place_remove`; `mc_set_waypoint`'s
`place`), prompt `agent-minecraft-dave-v7`.

**P2 remaining (checked against the code, 2026-10-13):** goals, the progress log and
memory reach Dave in game (the door), scoped to the asker's xuid. Left: the index tools
(they wait on M4), and `auto` log entries for crafting.

**P2 auto deaths (built 2026-10-13):** the session drain, which already puts each death
on the travel log's timeline, also writes an `auto` entry in that player's progress log
(`app.mc_goal_log`, on no goal, at the death's time, keyed like the timeline by xuid):
plain words, not Dave's, e.g. "Died at 10, 70, -4 in the Nether (burned in lava)" or
"Died (killed by a zombie)", from the place and the cause and killer the pack reports
(`sessions.death_entry`; anything that isn't a game id is dropped, and it fits the
500-character cap). It is written only when the death is new to the timeline, so the
drain re-reading events never repeats it, and through the drain's owner-scoped session
under the table's existing RLS. Tests: `tests/unit/test_mc_death_journal.py` and
`tests/integration/test_mc_travel_log_pg.py`.

**P3 (2026-10-11):** `mc_show_map` and the `minecraft_map` card in Minecraft_Dave's chat
(§M8, *Viewer, wave 3*), prompt `agent-minecraft-dave-v8`. Not in game: the in-game tier
leaves it out by name and the handler refuses a door turn.

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
| ★ `find_container(item)` — **shipped as `mc_find_item`** (2026-10-12, §M23) | "which chest has my diamonds?" | index | Player |
| ★ `where_is(player)` — **shipped as `mc_players` with a `gamertag`** (2026-10-11, §M18) | another player's position (sharing is on, owner decision) | live → index | Player |
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
| `waypoint_save(name)` / `list` / `delete` / `share` — **shipped as `mc_place_save` / `mc_places` / `mc_place_remove`** (2026-10-11, §M9; sharing not yet) | "remember this as *home*"; "where's Josh's mine?" | `app.mc_places` (per world folder, per player) | Player |
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
   (crafted from 8 Ender Pearls around a Blaze Powder), and one pack is used up. The tricorder is always kept. The approach below is the one
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
