// The Minecraft screen's derived state and copy (DESIGN.md "Minecraft server screen"), kept
// pure so the screen, its Ops shortcut row, the launcher tile and Ops' other stop paths all
// say the same thing — README finding 16 is exactly the bug of two paths wording one act
// differently.

import type {
  MinecraftServer,
  MinecraftStatus,
  MinecraftUpdate,
  MinecraftVersion,
} from "./api/client";

/** The compose service name Ops' generic service list shows the container under. */
export const MC_SERVICE = "minecraft";

/** `restarting` is the screen's word for the stopping → stopped → starting a Restart passes
 *  through, which would otherwise read as "stopping" and alarm whoever pressed it. `absent`
 *  is no container, `down` a container that isn't running (a deploy-level problem), and
 *  `unreachable` a running container whose wrapper won't answer. */
export type McState =
  | MinecraftServer["state"]
  | "restarting"
  | "absent"
  | "down"
  | "unreachable"
  | "unknown";

export type McLevel = "ok" | "warn" | "bad" | "off";

export function serverState(status: MinecraftStatus | null, restarting: boolean): McState {
  if (status === null) return "unknown";
  if (status.container === null) return "absent";
  if (status.server === null) return status.container.state === "running" ? "unreachable" : "down";
  const state = status.server.state;
  if (restarting && (state === "stopping" || state === "stopped" || state === "starting")) {
    return "restarting";
  }
  return state;
}

export function stateInfo(
  state: McState,
  server: MinecraftServer | null,
): { level: McLevel; word: string; sub: string } {
  switch (state) {
    case "running":
      return {
        level: "ok",
        word: "running",
        sub: server?.uptime_s != null ? `up ${fmtUptime(server.uptime_s)}` : "",
      };
    case "starting":
      return { level: "warn", word: "starting", sub: "loading the world…" };
    case "restarting":
      return { level: "warn", word: "restarting", sub: "saving the world, then starting again" };
    case "stopping":
      return { level: "warn", word: "stopping", sub: "saving the world — up to 60 s" };
    case "stopped":
      return { level: "off", word: "stopped", sub: "nobody can join" };
    case "installing":
      return {
        level: "warn",
        word: "installing",
        sub: server?.version
          ? `first boot — downloading ${server.version}`
          : "first boot — downloading the server",
      };
    case "install_failed":
      return { level: "bad", word: "install failed", sub: "nothing is installed yet" };
    case "absent":
      return { level: "off", word: "not set up", sub: "no Minecraft container on this box yet" };
    case "down":
      return { level: "bad", word: "container down", sub: "Start brings it up first" };
    case "unreachable":
      return { level: "bad", word: "unreachable", sub: "the container runs but won't answer" };
    default:
      return { level: "off", word: "—", sub: "" };
  }
}

const ACTIVE_UPDATE = new Set(["backing_up", "downloading", "restarting"]);

export function updateRunning(update: MinecraftUpdate | null | undefined): boolean {
  return update != null && ACTIVE_UPDATE.has(update.state);
}

/** An update is waiting: clients newer than the server are locked out until it lands.
 *  Decided by the versions, not the last update's state: the wrapper keeps that state for
 *  the container's life, so a `done` describes only its own `to` — a later release is
 *  still news. */
export function isBehind(
  version: MinecraftVersion | null,
  update: MinecraftUpdate | null | undefined,
): boolean {
  if (!version?.latest) return false;
  const waiting =
    version.update_available || (version.running !== null && version.latest !== version.running);
  // The version check can lag a just-finished update by a poll; that update covers `latest`.
  return waiting && !(update?.state === "done" && update.to === version.latest);
}

// ---- durations a person reads ----

/** Minutes, then `14h 05m`, then whole hours for play totals (`812 h`). */
export function fmtDur(seconds: number): string {
  const m = Math.max(0, Math.floor(seconds / 60));
  if (m < 60) return `${m} min`;
  if (m < 24 * 60) return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.round(m / 60)} h`;
}

/** Uptime runs for weeks, so above a day it counts days (`41 d 4 h`). */
export function fmtUptime(seconds: number): string {
  const m = Math.max(0, Math.floor(seconds / 60));
  if (m < 24 * 60) return fmtDur(seconds);
  return `${Math.floor(m / 1440)} d ${Math.floor((m % 1440) / 60)} h`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function listOf(names: string[]): string {
  return new Intl.ListFormat("en", { style: "long", type: "conjunction" }).format(names);
}

export function clockOf(epochS: number): string {
  return new Date(epochS * 1000)
    .toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })
    .toLowerCase();
}

export function shortDate(epochS: number): string {
  return new Date(epochS * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** `today`, `yesterday`, else `Oct 8` — lowercase, per the review's nit. */
export function dayOf(epochS: number, nowMs: number): string {
  const day = new Date(epochS * 1000);
  const today = new Date(nowMs);
  const startOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diff = Math.round((startOf(today) - startOf(day)) / 86_400_000);
  if (diff === 0) return "today";
  if (diff === 1) return "yesterday";
  return shortDate(epochS);
}

export function agoOf(epochS: number, nowMs: number): string {
  const m = Math.floor((nowMs / 1000 - epochS) / 60);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  return `${fmtDur(nowMs / 1000 - epochS)} ago`;
}

/** Mojang titles its articles by the marketing number: BDS 1.26.60.4 is "26.60". */
export function marketingNumber(version: string): string {
  return version.split(".").slice(1, 3).join(".");
}

// ---- confirms ----

export type ConfirmKind = "stop" | "restart" | "update" | "all";

export interface ConfirmContext {
  /** Gamertags of everyone on now. */
  online: string[];
  running: string | null;
  latest: string | null;
  /** Auto-update is on and an update is waiting, so a (re)start installs it first. */
  restartUpdates: boolean;
  /** The server is stopped, so an update installs and leaves it stopped. */
  stopped: boolean;
}

export interface ConfirmSpec {
  title: string;
  body: string;
  confirmLabel: string;
  tone: "danger" | "warn";
}

/** The Dialog for an act, or null when it should act at once. Stop and Restart confirm only
 *  when someone is on (or when a restart would also update); Update always confirms. */
export function confirmFor(kind: ConfirmKind, ctx: ConfirmContext): ConfirmSpec | null {
  const n = ctx.online.length;
  const who = listOf(ctx.online);
  const backup = `pre-update-${ctx.running ?? "current"}`;
  if (kind === "stop") {
    if (n === 0) return null;
    return {
      title: "Stop the server?",
      body: `${plural(n, "player")} — ${who} — will be disconnected; the world is saved first.`,
      confirmLabel: "Stop",
      tone: "danger",
    };
  }
  if (kind === "restart") {
    if (ctx.restartUpdates) {
      return {
        title: `Restart and update to ${ctx.latest}?`,
        body: n
          ? `Auto-update is on, so this restart also installs ${ctx.latest} after a backup (${backup}) — ${plural(n, "player")} — ${who} — will be disconnected for about a minute.`
          : `Auto-update is on, so this restart also installs ${ctx.latest} after a backup (${backup}); nobody is on.`,
        confirmLabel: "Restart",
        tone: "danger",
      };
    }
    if (n === 0) return null;
    return {
      title: "Restart the server?",
      body: `${plural(n, "player")} — ${who} — will be disconnected for about a minute; the world is saved first.`,
      confirmLabel: "Restart",
      tone: "danger",
    };
  }
  if (kind === "update") {
    return {
      title: `Update Minecraft to ${ctx.latest}?`,
      body: ctx.stopped
        ? `A backup (${backup}) is taken first, then ${ctx.latest} is installed — the server stays stopped until you start it.`
        : `A backup (${backup}) is taken first and players stay on while ${ctx.latest} downloads — then the server restarts and ${n ? `${plural(n, "player")} ${n === 1 ? "is" : "are"}` : "anyone on is"} disconnected for about a minute.`,
      confirmLabel: "Update",
      tone: "warn",
    };
  }
  if (n === 0 && !ctx.restartUpdates) return null;
  return {
    title: "Restart every service?",
    body: ctx.restartUpdates
      ? `That includes Minecraft, and auto-update is on, so it also installs ${ctx.latest} after a backup (${backup})${n ? ` — ${plural(n, "player")} (${who}) will be disconnected for about a minute` : ""}.`
      : `That includes Minecraft — ${plural(n, "player")} (${who}) will be disconnected for about a minute.`,
    confirmLabel: "Restart all",
    tone: "danger",
  };
}

export function confirmContext(
  status: MinecraftStatus | null,
  version: MinecraftVersion | null,
): ConfirmContext {
  const server = status?.server ?? null;
  const update = server?.update;
  return {
    online: (server?.players ?? []).map((p) => p.name),
    running: server?.version ?? version?.running ?? null,
    latest: version?.latest ?? null,
    restartUpdates:
      server?.auto_update === true && isBehind(version, update) && !updateRunning(update),
    stopped: server?.state === "stopped",
  };
}

// ---- the glance: Ops shortcut row and launcher tile ----

export interface Glance {
  level: McLevel;
  word: string;
  meta: string;
  tone: "" | "warn" | "bad";
}

/** The Ops Minecraft tile's word: who's on, or the lockout when an update waits. */
export function glanceOf(
  status: MinecraftStatus | null,
  version: MinecraftVersion | null,
  error: string | null,
): Glance {
  if (status === null) {
    return error
      ? {
          level: "bad",
          word: "unreachable",
          meta: `can't reach the server — ${error}`,
          tone: "bad",
        }
      : { level: "off", word: "—", meta: "checking…", tone: "" };
  }
  const state = serverState(status, false);
  const server = status.server;
  const { level, word } = stateInfo(state, server);
  const update = server?.update;
  const latest = version?.latest ?? update?.to ?? "";
  const running = server?.version ?? version?.running ?? null;
  if (state === "absent") return { level, word, meta: "not set up on this box", tone: "" };
  if (state === "unreachable") {
    return { level, word, meta: status.server_error ?? "the wrapper isn't answering", tone: "bad" };
  }
  if (updateRunning(update)) return { level, word, meta: `updating to ${latest}…`, tone: "warn" };
  if (update?.state === "failed") {
    return { level, word, meta: "update failed — newer clients still can't join", tone: "bad" };
  }
  if (update?.state === "rolled_back") {
    return {
      level,
      word,
      meta: `rolled back to ${update.from ?? running} — newer clients still can't join`,
      tone: "bad",
    };
  }
  if (isBehind(version, update)) {
    return {
      level,
      word,
      meta: `update available · ${latest} — newer clients can't join`,
      tone: "warn",
    };
  }
  if (state === "running") {
    const names = (server?.players ?? []).map((p) => p.name);
    return {
      level,
      word,
      meta: `${running ?? "—"} · ${names.length ? `${listOf(names)} on` : "nobody on"}`,
      tone: "",
    };
  }
  if (state === "installing") return { level, word, meta: "first boot · downloading", tone: "" };
  if (state === "install_failed") {
    return { level, word, meta: "download failed — open to retry", tone: "bad" };
  }
  return { level, word, meta: running ?? "—", tone: "" };
}

/** The launcher tile's dot and word: flags an update or a failure, otherwise the state. */
export function tileOf(
  status: MinecraftStatus | null,
  version: MinecraftVersion | null,
): { level: McLevel; word: string } | null {
  if (status === null) return null;
  const state = serverState(status, false);
  const update = status.server?.update;
  if (update?.state === "failed") return { level: "bad", word: "update failed" };
  if (update?.state === "rolled_back") return { level: "bad", word: "rolled back" };
  if (state === "install_failed") return { level: "bad", word: "install failed" };
  if (isBehind(version, update) && !updateRunning(update)) return { level: "warn", word: "update" };
  const { level, word } = stateInfo(state, status.server);
  return { level, word };
}
