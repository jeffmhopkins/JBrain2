// Minecraft API fixtures shaped exactly like the M1 contract — the mock's shared fixture
// (docs/mocks/minecraft-ops/README.md), so a test reads like the reviewed state it covers.
// Times are relative to "now" so session clocks read the same whenever the suite runs.

import type {
  MinecraftAllowlist,
  MinecraftBackup,
  MinecraftJob,
  MinecraftPlayer,
  MinecraftPlayers,
  MinecraftRuleValue,
  MinecraftRules,
  MinecraftServer,
  MinecraftServerSettings,
  MinecraftSlot,
  MinecraftStatus,
  MinecraftUpdate,
  MinecraftVersion,
  MinecraftWorlds,
} from "./api/client";
import { DEFAULT_RULES } from "./minecraftWorlds";

export const RUNNING = "1.26.52.3";
export const LATEST = "1.26.60.4";

const nowS = () => Date.now() / 1000;

export function mcUpdate(overrides: Partial<MinecraftUpdate> = {}): MinecraftUpdate {
  return {
    state: "idle",
    from: null,
    to: null,
    backup: null,
    error: null,
    started_at: 0,
    finished_at: null,
    ...overrides,
  };
}

export function mcServer(overrides: Partial<MinecraftServer> = {}): MinecraftServer {
  return {
    state: "running",
    run: true,
    install_error: null,
    version: RUNNING,
    level_name: "world",
    server_name: "JBrain",
    gamemode: "survival",
    difficulty: "normal",
    allow_list: false,
    lan_ip: "192.168.1.40",
    port: 19132,
    uptime_s: 192 * 60,
    players: [
      { name: "BlockyFox", xuid: "2535411", joined_at: nowS() - 42 * 60 },
      { name: "Mira_P", xuid: "2535412", joined_at: nowS() - 7 * 60 },
    ],
    update: mcUpdate(),
    auto_update: false,
    ...overrides,
  };
}

/** The game server stopped inside a running container — still a full `server`. */
export function mcStopped(overrides: Partial<MinecraftServer> = {}): MinecraftServer {
  return mcServer({ state: "stopped", run: false, players: [], uptime_s: null, ...overrides });
}

/** `server: null` is a deploy-level problem now (container down), never a plain stop. */
export function mcStatus(server: MinecraftServer | null = mcServer()): MinecraftStatus {
  return {
    container: {
      state: server ? "running" : "exited",
      health: server ? "healthy" : null,
      started_at: "2026-10-10T14:43:46Z",
    },
    server,
    server_error: null,
  };
}

export function mcVersion(overrides: Partial<MinecraftVersion> = {}): MinecraftVersion {
  return {
    running: RUNNING,
    latest: RUNNING,
    update_available: false,
    checked_at: nowS() - 12 * 60,
    check_error: null,
    notes: null,
    notes_fallback_url: "https://aka.ms/MinecraftUpdate",
    ...overrides,
  };
}

export const NOTES = {
  version: LATEST,
  title: "Minecraft Bedrock Edition 26.60 Changelog",
  url: "https://feedback.minecraft.net/hc/en-us/articles/26-60",
  published_at: "2026-10-08T17:00:00Z",
  lines: [
    "Fixed an issue where Copper Golems stopped sorting items after the world was reloaded",
    "Mobs can no longer push players through closed trapdoors",
    "Fixed a crash that could occur when joining a server while a Happy Ghast was on a lead",
    "Improved world loading times on dedicated servers",
  ],
};

export function mcBehind(overrides: Partial<MinecraftVersion> = {}): MinecraftVersion {
  return mcVersion({ latest: LATEST, update_available: true, notes: NOTES, ...overrides });
}

function player(
  gamertag: string,
  totalMin: number,
  sessions: number,
  online: boolean,
  lastAgoH: number,
): MinecraftPlayer {
  return {
    xuid: `x-${gamertag}`,
    gamertag,
    online,
    session_started_at: online ? nowS() - 42 * 60 : null,
    total_seconds: totalMin * 60,
    sessions,
    first_seen: nowS() - 8 * 86400,
    last_seen: nowS() - lastAgoH * 3600,
  };
}

export function mcPlayers(statsAvailable = false): MinecraftPlayers {
  return {
    players: [
      player("BlockyFox", 14 * 60 + 5, 19, true, 0),
      player("Mira_P", 9 * 60 + 40, 12, false, 1),
      player("Steve42", 3 * 60 + 15, 5, false, 30),
      player("Pebble_J", 48, 2, false, 24 * 4),
    ],
    stats_available: statsAvailable,
  };
}

/** The `dev:mock` states, picked with `?mc=` on the page URL: running (default), empty,
 *  stopped, updating, failed, rolled_back, installing, install_failed, down. */
export function minecraftScenario(name: string | null): {
  status: MinecraftStatus;
  version: MinecraftVersion;
  players: MinecraftPlayers;
} {
  const nobody: MinecraftPlayers = { players: [], stats_available: false };
  const nowS = Date.now() / 1000;
  const upd = (state: MinecraftUpdate["state"], extra: Partial<MinecraftUpdate> = {}) =>
    mcUpdate({
      state,
      from: RUNNING,
      to: LATEST,
      backup: `world-pre-update-${RUNNING}.mcworld`,
      started_at: nowS - 90,
      ...extra,
    });
  switch (name) {
    case "empty":
      return {
        status: mcStatus(mcServer({ players: [] })),
        version: mcVersion(),
        players: mcPlayers(),
      };
    case "stopped":
      return { status: mcStatus(mcStopped()), version: mcBehind(), players: mcPlayers() };
    case "updating":
      return {
        status: mcStatus(mcServer({ update: upd("downloading") })),
        version: mcBehind(),
        players: mcPlayers(),
      };
    case "failed":
      return {
        status: mcStatus(
          mcServer({
            update: upd("failed", {
              error: `bedrock-server-${LATEST}.zip — checksum mismatch after download`,
              finished_at: nowS - 30,
            }),
          }),
        ),
        version: mcBehind(),
        players: mcPlayers(),
      };
    case "rolled_back":
      return {
        status: mcStatus(
          mcServer({
            players: [],
            uptime_s: 180,
            update: upd("rolled_back", {
              error: `bedrock_server ${LATEST} didn't start within 2 minutes`,
              finished_at: nowS - 30,
            }),
          }),
        ),
        version: mcBehind(),
        players: mcPlayers(),
      };
    case "installing":
      return {
        status: mcStatus(
          mcServer({ state: "installing", version: null, players: [], uptime_s: null }),
        ),
        version: mcVersion(),
        players: nobody,
      };
    case "install_failed":
      return {
        status: mcStatus(
          mcServer({
            state: "install_failed",
            version: null,
            players: [],
            uptime_s: null,
            install_error: `bedrock-server-${RUNNING}.zip — download failed: HTTP 503 from www.minecraft.net`,
          }),
        ),
        version: mcVersion(),
        players: nobody,
      };
    case "down":
      return { status: mcStatus(null), version: mcBehind(), players: mcPlayers() };
    default:
      return { status: mcStatus(), version: mcBehind(), players: mcPlayers() };
  }
}

// ---- worlds and backups: the worlds mock's shared fixture (docs/mocks/minecraft-worlds/
// README.md). Castle Hill is loaded; Skyblock run is created but not generated; slot 5 is
// empty. ----

const MB = 1024 * 1024;
const daysAgo = (d: number, h = 0) => nowS() - d * 86400 - h * 3600;

export function mcSlot(overrides: Partial<MinecraftSlot> = {}): MinecraftSlot {
  return {
    id: "slot1",
    folder: "world",
    name: "World",
    exists: true,
    active: false,
    seed: "-2794311108712645813",
    gamemode: "survival",
    difficulty: "normal",
    cheats: false,
    origin: null,
    created_at: null,
    last_loaded: null,
    bytes: 3.1 * MB,
    last_played: daysAgo(6),
    backups: 1,
    last_backup: daysAgo(6),
    last_download: null,
    ...overrides,
  };
}

export const CASTLE_DOWNLOAD = daysAgo(6, -1);

export function mcSlots(): MinecraftSlot[] {
  return [
    mcSlot(),
    mcSlot({
      id: "slot2",
      folder: "slot2",
      name: "Castle Hill",
      active: true,
      seed: "-6104328617705162911",
      origin: "imported",
      created_at: daysAgo(6),
      bytes: 7.8 * MB,
      last_played: nowS() - 60,
      backups: 4,
      last_backup: daysAgo(1),
      last_download: CASTLE_DOWNLOAD,
    }),
    mcSlot({
      id: "slot3",
      folder: "slot3",
      name: "Creative test",
      seed: "8675309",
      gamemode: "creative",
      difficulty: "peaceful",
      cheats: true,
      origin: "new",
      created_at: daysAgo(3),
      bytes: 5.2 * MB,
      last_played: daysAgo(3),
      backups: 1,
      last_backup: daysAgo(3),
    }),
    mcSlot({
      id: "slot4",
      folder: "slot4",
      name: "Skyblock run",
      exists: false,
      seed: "5127438807419962211",
      difficulty: "hard",
      origin: "new",
      created_at: daysAgo(1),
      bytes: 0,
      last_played: null,
      backups: 0,
      last_backup: null,
    }),
    mcSlot({
      id: "slot5",
      folder: "slot5",
      name: null,
      exists: false,
      seed: null,
      bytes: 0,
      last_played: null,
      backups: 0,
      last_backup: null,
    }),
  ];
}

export function mcWorlds(slots: MinecraftSlot[] = mcSlots()): MinecraftWorlds {
  return { active: slots.find((s) => s.active)?.folder ?? "world", keep_per_slot: 20, slots };
}

export function mcBackup(overrides: Partial<MinecraftBackup> = {}): MinecraftBackup {
  return {
    name: "slot2-20261009-191200-before-the-castle-roof.mcworld",
    bytes: 7.6 * MB,
    created: daysAgo(1),
    folder: "slot2",
    label: "before the castle roof",
    auto: false,
    pinned: false,
    downloaded_at: null,
    note: "",
    ...overrides,
  };
}

/** Castle Hill's four: a pinned one of the owner's, two automatic, and the downloaded one. */
export function mcBackups(): MinecraftBackup[] {
  return [
    mcBackup({ pinned: true }),
    mcBackup({
      name: "slot2-20261007-160200-pre-load.mcworld",
      label: "pre-load",
      note: "before loading Creative test",
      auto: true,
      created: daysAgo(3, 2),
      bytes: 7.3 * MB,
    }),
    mcBackup({
      name: "slot2-20261006-093000-pre-update-1.26.51.1.mcworld",
      label: "pre-update-1.26.51.1",
      auto: true,
      created: daysAgo(4),
      bytes: 7.1 * MB,
    }),
    mcBackup({
      name: "slot2-20261004-201500-first-night.mcworld",
      label: "first night",
      created: daysAgo(6),
      bytes: 6.4 * MB,
      downloaded_at: CASTLE_DOWNLOAD,
    }),
  ];
}

/** A world at the 20-backup limit: the four above (one pinned) plus 17 automatic ones. */
export function mcBackupsAtLimit(): MinecraftBackup[] {
  const extra = Array.from({ length: 17 }, (_, i) =>
    mcBackup({
      name: `slot2-202609${String(28 - i).padStart(2, "0")}-pre-load.mcworld`,
      label: "pre-load",
      auto: true,
      created: daysAgo(8 + i),
      bytes: 6.1 * MB,
    }),
  );
  return [...mcBackups(), ...extra];
}

export function mcRules(
  slot = "slot2",
  live = true,
  overrides: Record<string, MinecraftRuleValue> = {},
  pending: string[] = [],
): MinecraftRules {
  return {
    slot,
    live,
    rules: { ...DEFAULT_RULES, ...overrides },
    defaults: { ...DEFAULT_RULES },
    pending,
  };
}

export function mcJob(overrides: Partial<MinecraftJob> = {}): MinecraftJob {
  return { what: "importing a world", phase: "writing", started_at: nowS() - 42, ...overrides };
}

export function mcAllowlist(overrides: Partial<MinecraftAllowlist> = {}): MinecraftAllowlist {
  return {
    enabled: true,
    players: ["BlockyFox", "Mira_P", "Pebble_J", "Steve42"],
    ...overrides,
  };
}

/** Numbers since the backend's fix; the screen still tolerates the old text form. */
export function mcServerSettings(): MinecraftServerSettings {
  return { server_name: "JBrain", max_players: 10, view_distance: 32 };
}

/** The `dev:mock` worlds states, picked with `?mcw=` on the page URL: default, fresh,
 *  seed_unknown, importing, loading, limit, pending. */
export function minecraftWorldsScenario(name: string | null): {
  worlds: MinecraftWorlds;
  backups: MinecraftBackup[];
  job: MinecraftJob | null;
} {
  switch (name) {
    case "fresh": {
      const [first, ...rest] = mcSlots();
      const empty = rest.map((s) =>
        mcSlot({ ...s, name: null, exists: false, seed: null, bytes: 0, backups: 0 }),
      );
      return {
        worlds: mcWorlds([mcSlot({ ...first, active: true, backups: 0 }), ...empty]),
        backups: [],
        job: null,
      };
    }
    case "seed_unknown":
      return {
        worlds: mcWorlds(mcSlots().map((s) => (s.id === "slot2" ? { ...s, seed: null } : s))),
        backups: mcBackups(),
        job: null,
      };
    case "importing":
      return { worlds: mcWorlds(), backups: mcBackups(), job: mcJob() };
    case "loading":
      return {
        worlds: mcWorlds(),
        backups: mcBackups(),
        job: mcJob({ what: "loading a world", phase: "warning players", started_at: nowS() - 4 }),
      };
    case "limit":
      return { worlds: mcWorlds(), backups: mcBackupsAtLimit(), job: null };
    default:
      return { worlds: mcWorlds(), backups: mcBackups(), job: null };
  }
}
