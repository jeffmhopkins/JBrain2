// The words GrowBot's surfaces share — the screen, the Add a robot sheet, the launcher tile and
// the Ops row — so a robot reads the same wherever the owner meets it (binding mock
// docs/mocks/growbot-flash/b-growbot-screen.html, copy from its shared.js).

import type { GrowbotFailCode, GrowbotFlashJob, GrowbotRobot, GrowbotStatus } from "./api/client";

export type GrowbotLevel = "ok" | "warn" | "bad";

export function ago(iso: string, now = Date.now()): string {
  const s = (now - Date.parse(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

export function day(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** A robot's state in words beside its dot; colour is never the only signal. */
export function robotState(
  r: GrowbotRobot,
  serviceUp: boolean,
  now = Date.now(),
): { level: GrowbotLevel; text: string } {
  if (r.revoked_at) return { level: "bad", text: "key revoked — can't connect" };
  if (!serviceUp) return { level: "bad", text: "can't connect — growbot is down" };
  const seen = r.last_seen_at ? ago(r.last_seen_at, now) : null;
  if (r.connected) return { level: "ok", text: seen ? `connected · ${seen}` : "connected" };
  return {
    level: "warn",
    text: seen ? `not connected · last seen ${seen}` : "not connected · never seen",
  };
}

/** The key's age is its last mint: a rotation replaced the one made at first flash. */
export function keyMadeAt(r: GrowbotRobot): string {
  return r.rotated_at ?? r.created_at;
}

/** The launcher tile's second line: who is connected, or that nobody can be. */
export function tileLine(s: GrowbotStatus): { level: GrowbotLevel | null; word: string } | null {
  // Read on every launcher open, from a box that may predate GrowBot: anything that isn't the
  // status shape leaves the tile without a line rather than taking the launcher down.
  if (!s || !Array.isArray(s.robots)) return null;
  if (s.service === "down") return { level: "bad", word: "growbot is down" };
  const on = s.robots.filter((r) => r.connected && !r.revoked_at);
  if (on.length === 0) return { level: null, word: "no robot connected" };
  return { level: "ok", word: `${on.map((r) => r.name || r.code).join(", ")} connected` };
}

export const PHASES: { id: keyof GrowbotFlashJob["phases"]; label: string }[] = [
  { id: "load", label: "Load MicroPython" },
  { id: "copy", label: "Copy its firmware and Wi-Fi" },
  { id: "join", label: "Join Wi-Fi and reach the box" },
];

export const PHASE_WORD: Record<GrowbotFlashJob["phases"]["load"], string> = {
  wait: "waiting",
  run: "running",
  done: "done",
  fail: "failed",
  skip: "already on it",
};

/** What happened and what to do next, per failure — never "see the log" (DESIGN.md Voice &
 *  microcopy). load_failed and wifi_failed are the mock's own words. */
export const FAIL_COPY: Record<GrowbotFailCode, { title: string; body: string }> = {
  load_failed: {
    title: "MicroPython did not load",
    body: "the Pico isn't in its USB loader. Unplug it, hold the white BOOTSEL button while plugging it back into the box, then flash again. Nothing on it was changed.",
  },
  not_bootsel: {
    title: "It wouldn't restart into its USB loader",
    body: "the box asked the Pico to, and it stayed as it was. Unplug it, hold the white BOOTSEL button while plugging it back into the box, then flash again. Nothing on it was changed.",
  },
  usb_gone: {
    title: "It left the box's USB partway through",
    body: "the Pico disconnected while it was being flashed. Check it is on a data cable and pushed fully in, plug it back into the box, then flash again.",
  },
  wrong_robot: {
    title: "That's a different robot",
    body: "the Pico on the cable has another pairing code than the robot you chose, so nothing was written and no key was made. Pick the robot that's plugged in, or add it as a new one.",
  },
  copy_failed: {
    title: "Its files didn't copy",
    body: "MicroPython is on it, but copying its firmware and Wi-Fi stopped partway. Unplug it, plug it back into the box, then flash again; the next try skips MicroPython.",
  },
  wifi_failed: {
    title: "Copied, but it couldn't join Wi-Fi",
    body: "none of its networks answered. Check they are 2.4 GHz and in range of the robot, then flash again; MicroPython is already on it, so the next try is quicker.",
  },
  box_unreached: {
    title: "It joined Wi-Fi but never reached the box",
    body: "it is on the network, and its call to the box didn't arrive. Check that network can reach the box — a guest network usually can't — then flash again.",
  },
  timeout: {
    title: "It stopped answering",
    body: "the robot went quiet before it finished. Unplug it, plug it back into the box, then flash again.",
  },
  internal: {
    title: "The box hit a fault of its own",
    body: "nothing about the robot caused this. Flash again; if it keeps happening, restart api from Ops → Services.",
  },
};

/** Shown once per phone: the box's certificate note before the first Open GrowBot. */
export const CERT_NOTED_KEY = "jb.growbot.certNoted";

export function certNoted(): boolean {
  try {
    return localStorage.getItem(CERT_NOTED_KEY) === "1";
  } catch {
    return false;
  }
}

export function noteCert(): void {
  try {
    localStorage.setItem(CERT_NOTED_KEY, "1");
  } catch {
    // A private window keeps no note, so the next open explains the certificate again.
  }
}

export const CERT_URL = "/api/lan/root.crt";
