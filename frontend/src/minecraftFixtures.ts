// Minecraft API fixtures shaped exactly like the M1 contract — the mock's shared fixture
// (docs/mocks/minecraft-ops/README.md), so a test reads like the reviewed state it covers.
// Times are relative to "now" so session clocks read the same whenever the suite runs.

import type {
  MinecraftPlayer,
  MinecraftPlayers,
  MinecraftServer,
  MinecraftStatus,
  MinecraftUpdate,
  MinecraftVersion,
} from "./api/client";

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
