import { describe, expect, it } from "vitest";
import {
  confirmContext,
  confirmFor,
  dayOf,
  fmtDur,
  fmtUptime,
  glanceOf,
  isBehind,
  marketingNumber,
  tileOf,
} from "./minecraft";
import {
  LATEST,
  RUNNING,
  mcBehind,
  mcServer,
  mcStatus,
  mcStopped,
  mcUpdate,
  mcVersion,
} from "./minecraftFixtures";

describe("durations a person reads", () => {
  it("minutes, then h+m, then whole hours", () => {
    expect(fmtDur(48 * 60)).toBe("48 min");
    expect(fmtDur((14 * 60 + 5) * 60)).toBe("14h 05m");
    expect(fmtDur(812 * 3600)).toBe("812 h");
  });

  it("uptime counts days above a day", () => {
    expect(fmtUptime(3 * 3600)).toBe("3h 00m");
    expect(fmtUptime((41 * 1440 + 4 * 60 + 12) * 60)).toBe("41 d 4 h");
  });

  it("days are lowercase today / yesterday, else a short date", () => {
    const now = new Date(2026, 9, 10, 15, 0).getTime();
    expect(dayOf(new Date(2026, 9, 10, 9, 0).getTime() / 1000, now)).toBe("today");
    expect(dayOf(new Date(2026, 9, 9, 20, 14).getTime() / 1000, now)).toBe("yesterday");
    expect(dayOf(new Date(2026, 9, 8, 18, 40).getTime() / 1000, now)).toBe("Oct 8");
  });

  it("finds Mojang's marketing number in a server version", () => {
    expect(marketingNumber(LATEST)).toBe("26.60");
  });
});

describe("behind", () => {
  it("a finished update to X doesn't hide a later release Y", () => {
    const doneToX = mcUpdate({ state: "done", from: RUNNING, to: LATEST });
    const yOut = mcVersion({ running: LATEST, latest: "1.26.70.1", update_available: true });
    expect(isBehind(yOut, doneToX)).toBe(true);
  });

  it("reads behind from the versions even if update_available lags", () => {
    expect(isBehind(mcVersion({ latest: LATEST, update_available: false }), mcUpdate())).toBe(true);
  });

  it("the update that just landed covers latest while the version check catches up", () => {
    const done = mcUpdate({ state: "done", from: RUNNING, to: LATEST });
    expect(isBehind(mcBehind(), done)).toBe(false);
  });

  it("is not behind when running is latest", () => {
    expect(isBehind(mcVersion(), mcUpdate())).toBe(false);
  });
});

describe("confirms", () => {
  const empty = confirmContext(mcStatus(mcServer({ players: [] })), mcVersion());

  it("Stop and Restart act at once with nobody on", () => {
    expect(confirmFor("stop", empty)).toBeNull();
    expect(confirmFor("restart", empty)).toBeNull();
    expect(confirmFor("all", empty)).toBeNull();
  });

  it("Update always confirms and names the backup", () => {
    expect(confirmFor("update", { ...empty, latest: LATEST })?.body).toContain(
      `A backup (pre-update-${RUNNING}) is taken first`,
    );
  });

  it("one player reads in the singular", () => {
    const one = confirmContext(
      mcStatus(mcServer({ players: [{ name: "Steve42", xuid: "1", joined_at: 0 }] })),
      mcVersion(),
    );
    expect(confirmFor("stop", one)?.body).toBe(
      "1 player — Steve42 — will be disconnected; the world is saved first.",
    );
  });

  it("Restart all with auto-update on and an update waiting names the update", () => {
    const ctx = confirmContext(mcStatus(mcServer({ players: [], auto_update: true })), mcBehind());
    expect(confirmFor("all", ctx)?.body).toContain(`also installs ${LATEST}`);
  });

  it("a running update doesn't make Restart an update", () => {
    const ctx = confirmContext(
      mcStatus(
        mcServer({ players: [], auto_update: true, update: mcUpdate({ state: "downloading" }) }),
      ),
      mcBehind(),
    );
    expect(ctx.restartUpdates).toBe(false);
  });
});

describe("the glance", () => {
  it("reads who's on, or nobody", () => {
    expect(glanceOf(mcStatus(), mcVersion(), null).meta).toBe(
      `${RUNNING} · BlockyFox and Mira_P on`,
    );
    expect(glanceOf(mcStatus(mcServer({ players: [] })), mcVersion(), null).meta).toBe(
      `${RUNNING} · nobody on`,
    );
  });

  it("puts the lockout first while an update waits", () => {
    const g = glanceOf(mcStatus(), mcBehind(), null);
    expect(g.meta).toBe(`update available · ${LATEST} — newer clients can't join`);
    expect(g.tone).toBe("warn");
  });

  it("says an update is running, and a failure keeps the lockout", () => {
    const running = mcServer({ update: mcUpdate({ state: "downloading", to: LATEST }) });
    expect(glanceOf(mcStatus(running), mcBehind(), null).meta).toBe(`updating to ${LATEST}…`);
    const failed = mcServer({ update: mcUpdate({ state: "failed", to: LATEST }) });
    expect(glanceOf(mcStatus(failed), mcBehind(), null).meta).toBe(
      "update failed — newer clients still can't join",
    );
  });

  it("claims no progress figure while installing", () => {
    const g = glanceOf(mcStatus(mcServer({ state: "installing", version: null })), null, null);
    expect(g.meta).toBe("first boot · downloading");
  });

  it("reports an unreachable server", () => {
    expect(glanceOf(null, null, "sidecar unreachable").meta).toBe(
      "can't reach the server — sidecar unreachable",
    );
  });

  it("the launcher tile flags an update or a failure over the state word", () => {
    expect(tileOf(mcStatus(), mcVersion())).toEqual({ level: "ok", word: "running" });
    expect(tileOf(mcStatus(), mcBehind())).toEqual({ level: "warn", word: "update" });
    expect(tileOf(mcStatus(mcServer({ state: "install_failed", version: null })), null)).toEqual({
      level: "bad",
      word: "install failed",
    });
    expect(tileOf(mcStatus(mcStopped()), mcVersion())).toEqual({ level: "off", word: "stopped" });
    expect(tileOf(mcStatus(null), mcVersion())).toEqual({ level: "bad", word: "container down" });
    const rolled = mcServer({ update: mcUpdate({ state: "rolled_back", from: RUNNING }) });
    expect(tileOf(mcStatus(rolled), mcBehind())).toEqual({ level: "bad", word: "rolled back" });
    expect(glanceOf(mcStatus(rolled), mcBehind(), null).meta).toBe(
      `rolled back to ${RUNNING} — newer clients still can't join`,
    );
    expect(tileOf(null, null)).toBeNull();
  });
});
