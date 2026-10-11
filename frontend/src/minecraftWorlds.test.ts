import { describe, expect, it } from "vitest";
import type { MinecraftSlot } from "./api/client";
import {
  mcBackup,
  mcBackups,
  mcBackupsAtLimit,
  mcRules,
  type mcSlot,
  mcSlots,
} from "./minecraftFixtures";
import {
  DEFAULT_RULES,
  DRAFT_KEEP_MODES,
  KEEP_HELP,
  KEEP_MODES,
  MAX_IMPORT_BYTES,
  PINNED_DELETE_WHY,
  RULE_GROUPS,
  autoReason,
  backupTitle,
  canDeleteBackup,
  changedRules,
  choiceOptions,
  clampRule,
  defaultResetMode,
  elapsedOf,
  fmtBytes,
  groupsFor,
  importCheck,
  importOverConfirm,
  isRefusal,
  jobLine,
  keepFailText,
  keepMeaning,
  keepMode,
  keepSaved,
  keepSavedToast,
  keepSteps,
  loadConfirm,
  nextToGo,
  originText,
  refusalReason,
  resetConfirm,
  resetOptions,
  restoreConfirm,
  restoreTargetText,
  restoredName,
  rulesNote,
  seedFor,
  settingsLine,
  worldBlocked,
} from "./minecraftWorlds";

const MB = 1024 * 1024;
const [world, castle, creative, skyblock, empty] = mcSlots() as [
  ReturnType<typeof mcSlot>,
  ReturnType<typeof mcSlot>,
  ReturnType<typeof mcSlot>,
  ReturnType<typeof mcSlot>,
  ReturnType<typeof mcSlot>,
];
const running = { running: true, online: ["BlockyFox", "Mira_P"] };
const nobody = { running: true, online: [] };
const stopped = { running: false, online: [] };

describe("game rules: live or pending", () => {
  it("is live only when the API says loaded AND running", () => {
    const note = rulesNote(mcRules("slot2", true), castle);
    expect(note.tone).toBe("live");
    expect(note.head).toBe("Loaded and running — changes apply live, at once.");
  });

  it("the loaded world with the server stopped waits for the start", () => {
    const note = rulesNote(mcRules("slot2", false), castle);
    expect(note.tone).toBe("wait");
    expect(note.head).toBe("Server stopped — changes are applied when it starts.");
  });

  it("a world that isn't loaded waits for its next load", () => {
    const note = rulesNote(mcRules("slot4", false, {}, ["keepInventory"]), skyblock);
    expect(note.head).toBe(
      "Not loaded — changes are saved and applied when Skyblock run next loads.",
    );
  });

  it("settings say the same thing", () => {
    expect(settingsLine(castle, true)).toMatch(/^Loaded and running — difficulty changes live/);
    expect(settingsLine(castle, false)).toMatch(/applied when the server starts/);
    expect(settingsLine(creative, true)).toMatch(/when Creative test next loads/);
  });

  it("counts the rules changed from the defaults", () => {
    expect(changedRules(mcRules("slot3", false, { doDayLightCycle: false, pvp: true }))).toEqual([
      "doDayLightCycle",
    ]);
  });

  it("never counts a rule the Power Pack holds as changed", () => {
    const view = mcRules("slot2", true, { keepInventory: true, pvp: false });
    expect(changedRules(view)).toEqual(["keepInventory", "pvp"]);
    expect(changedRules({ ...view, managed: { keepInventory: true } })).toEqual(["pvp"]);
  });

  it("groups every reported rule, and a new one lands in Other rather than vanishing", () => {
    expect(RULE_GROUPS.map((g) => g.title)).toEqual([
      "World",
      "Time & weather",
      "Players",
      "Mobs & drops",
      "Crafting",
      "Commands",
      "Display",
    ]);
    expect(Object.keys(DEFAULT_RULES)).toHaveLength(39);
    const groups = groupsFor({ ...DEFAULT_RULES, brandNewRule: true });
    expect(groups.at(-1)?.title).toBe("Other");
    expect(groups.at(-1)?.rules[0]?.id).toBe("brandNewRule");
  });

  it("playerWaypoints offers the current value plus everyone, and nothing guessed", () => {
    expect(choiceOptions("everyone")).toEqual(["everyone"]);
    expect(choiceOptions("none")).toEqual(["none", "everyone"]);
  });

  it("a stepper's value stays in its UI bounds", () => {
    const pct = RULE_GROUPS[1]?.rules.find((r) => r.id === "playersSleepingPercentage");
    if (!pct) throw new Error("missing rule");
    expect(clampRule(pct, 140)).toBe(100);
    expect(clampRule(pct, -5)).toBe(0);
  });
});

describe("seeds", () => {
  it("Random sends the number it showed", () => {
    expect(seedFor("random", "8675309", "ignored")).toBe("8675309");
  });

  it("Enter needs some text, at most 64 characters on one line", () => {
    expect(seedFor("enter", "1", "  kids castle ")).toBe("kids castle");
    expect(seedFor("enter", "1", "   ")).toBeNull();
    expect(seedFor("enter", "1", "x".repeat(65))).toBeNull();
    expect(seedFor("enter", "1", "a\nb")).toBeNull();
  });
});

describe("import size guards", () => {
  it("refuses over 1 GB before uploading", () => {
    expect(importCheck(1331 * MB)).toBe("too_big");
    expect(importCheck(MAX_IMPORT_BYTES)).toBe("tunnel");
  });

  it("warns over 100 MB, which only uploads at home", () => {
    expect(importCheck(300 * MB)).toBe("tunnel");
    expect(importCheck(9.4 * MB)).toBe("ok");
  });

  it("reads sizes like a person", () => {
    expect(fmtBytes(9.4 * MB)).toBe("9.4 MB");
    expect(fmtBytes(300 * MB)).toBe("300 MB");
    expect(fmtBytes(1331 * MB)).toBe("1.3 GB");
  });
});

describe("reset", () => {
  it("offers Same seed only when the seed is known", () => {
    const known = resetOptions(creative);
    expect(known.find((o) => o.mode === "same_seed")?.disabled).toBe(false);
    expect(defaultResetMode(creative)).toBe("same_seed");
    const unknown = resetOptions({ ...castle, seed: null });
    expect(unknown.find((o) => o.mode === "same_seed")).toMatchObject({
      disabled: true,
      sub: "Not offered — this world's seed is unknown (its level.dat couldn't be read).",
    });
    expect(defaultResetMode({ ...castle, seed: null })).toBe("new_seed");
  });

  it("never offers Empty for the loaded world", () => {
    expect(resetOptions(castle).find((o) => o.mode === "empty")?.disabled).toBe(true);
    expect(resetOptions(world).find((o) => o.mode === "empty")?.disabled).toBe(false);
  });

  it("names the chat warning when the loaded world has players on", () => {
    expect(resetConfirm(castle, "new_seed", running).body).toBe(
      "Castle Hill is backed up first, then replaced by a new world — everything built there leaves the slot, and BlockyFox and Mira_P get a 10-second warning in chat and are disconnected.",
    );
    expect(resetConfirm(world, "empty", running).confirmLabel).toBe("Empty slot");
  });
});

describe("load", () => {
  it("with players on is the destructive variant, in one sentence", () => {
    expect(loadConfirm(creative, castle, running)).toEqual({
      title: "Load Creative test?",
      body: "BlockyFox and Mira_P get a 10-second warning in chat and are disconnected; Castle Hill is backed up first, then Creative test starts.",
      confirmLabel: "Load",
      tone: "danger",
    });
  });

  it("with nobody on is a plain primary", () => {
    expect(loadConfirm(skyblock, castle, nobody)).toMatchObject({
      body: "The server stops, Castle Hill is backed up, then Skyblock run starts and is generated — nobody is on.",
      tone: "primary",
    });
  });

  it("while stopped takes no backup, so says none", () => {
    const spec = loadConfirm(creative, castle, stopped);
    expect(spec.body).toBe(
      "Creative test becomes the loaded world — the server stays stopped until you start it.",
    );
    expect(spec.body).not.toMatch(/backed up/);
  });
});

describe("import over and restore", () => {
  it("import over the loaded world with players on warns them", () => {
    const spec = importOverConfirm(castle, "Hilltop Village.mcworld", running);
    expect(spec.tone).toBe("danger");
    // The box names the world from the file's levelname.txt, so no name is promised.
    expect(spec.body).toBe(
      "Castle Hill is backed up first, then replaced by the world in Hilltop Village.mcworld, with the file's own name and settings — BlockyFox and Mira_P get a 10-second warning in chat and are disconnected until it's back up.",
    );
    expect(importOverConfirm(creative, "x.mcworld", running).tone).toBe("warn");
  });

  it("restoring another world's backup brings its seed and settings", () => {
    const b = mcBackup();
    expect(restoreTargetText(b, creative, castle, running)).toBe(
      "replaces Creative test — the backup's seed and settings come too; Creative test is backed up first",
    );
    expect(restoreTargetText(b, castle, castle, running)).toBe(
      "its own world — rolled back; Castle Hill is backed up first, and the server restarts around it",
    );
    expect(restoreTargetText(b, empty, castle, running)).toMatch(
      /^empty — becomes “Castle Hill \(\w+ \d+\)” with the backup's seed and settings and Castle Hill's rules$/,
    );
    expect(restoreConfirm(b, creative, running, Date.now()).body).toMatch(
      /, its seed and settings included\.$/,
    );
  });
});

describe("restore and errors", () => {
  it("says a world is backed up first only when it exists", () => {
    const b = mcBackup();
    expect(restoreTargetText(b, skyblock, castle, running)).toBe(
      "replaces Skyblock run — the backup's seed and settings come too",
    );
    expect(restoreConfirm(b, skyblock, running, Date.now()).body).toMatch(
      /^Skyblock run is replaced with “before the castle roof”/,
    );
    expect(restoreConfirm(b, creative, running, Date.now()).body).toMatch(
      /^Creative test is backed up first, then replaced with/,
    );
  });

  it("an empty slot is named as the box names it, by folder when the source has none", () => {
    const b = mcBackup();
    expect(restoredName(b, castle)).toMatch(/^Castle Hill \(\w+ \d+\)$/);
    expect(restoredName({ ...b, folder: "slot7" }, null)).toMatch(/^slot7 \(\w+ \d+\)$/);
  });

  it("only a refusal means nothing changed", () => {
    for (const code of [400, 409, 413, 422]) expect(isRefusal(code)).toBe(true);
    for (const code of [0, 500, 502, 503, 524]) expect(isRefusal(code)).toBe(false);
  });
});

describe("backups", () => {
  it("a pinned backup can't be deleted", () => {
    expect(canDeleteBackup(mcBackup({ pinned: true }))).toBe(false);
    expect(canDeleteBackup(mcBackup({ pinned: false }))).toBe(true);
    expect(PINNED_DELETE_WHY).toBe("Unpin it first — a pinned backup can't be deleted.");
  });

  it("names the automatic ones by their reason", () => {
    expect(autoReason("pre-update-1.26.51.1")).toBe("before the update from 1.26.51.1");
    expect(autoReason("pre-load")).toBe("before loading another world");
    expect(backupTitle(mcBackup({ label: "pre-reset", auto: true }))).toBe("Before a reset");
    expect(backupTitle(mcBackup({ label: "", auto: false }))).toBe("Your backup");
    // The box's own note wins when it has one.
    expect(
      backupTitle(
        mcBackup({ label: "pre-load", auto: true, note: "before loading Creative test" }),
      ),
    ).toBe("Before loading Creative test");
  });

  it("at the limit, the next backup removes the oldest automatic one; pinned never go", () => {
    expect(nextToGo(mcBackups(), 20)).toBeNull();
    const full = mcBackupsAtLimit();
    const goes = nextToGo(full, 20);
    expect(goes?.auto).toBe(true);
    expect(goes?.created).toBe(Math.min(...full.filter((b) => b.auto).map((b) => b.created)));
    const owners = full.filter((b) => !b.auto);
    expect(nextToGo(owners, 1)?.label).toBe("first night");
  });
});

describe("jobs", () => {
  it("shows what, phase and elapsed", () => {
    expect(jobLine({ what: "importing a world", phase: "writing", started_at: 0 })).toBe(
      "Importing a world — writing…",
    );
    expect(elapsedOf(1000, 1000 * 1000 + 65_000)).toBe("1:05");
  });

  it("blocks world actions while a job, an update or a lifecycle runs, and says why", () => {
    expect(
      worldBlocked({ what: "loading a world", phase: null, started_at: 1 }, false, "running"),
    ).toBe("Wait — the server is loading a world.");
    expect(worldBlocked(null, true, "running")).toMatch(/^Busy updating Minecraft/);
    expect(worldBlocked(null, false, "stopping")).toBe("Wait for the server to finish stopping.");
    expect(worldBlocked(null, false, "running")).toBe("");
  });

  it("a world the debug API made says so, so the owner can tell it from theirs", () => {
    const creative = mcSlots()[2] as MinecraftSlot;
    expect(originText({ ...creative, test: true })).toMatch(
      /^test world, \w+ \d+ — made over the debug API$/,
    );
    expect(originText(creative)).toMatch(/^new world, /);
  });
});

describe("keep inventory: Off · On · Power Pack", () => {
  const slots = mcSlots();
  const castle = slots[1] as MinecraftSlot;
  const sky = slots[3] as MinecraftSlot;

  it("a saved world offers Power Pack; the New world draft doesn't", () => {
    expect(KEEP_MODES).toEqual(["off", "on", "pack"]);
    expect(DRAFT_KEEP_MODES).toEqual(["off", "on"]);
  });

  it("reads the mode from the managed rule first", () => {
    const view = mcRules("slot2", true, { keepInventory: true });
    expect(keepMode(view)).toBe("on");
    expect(keepMode({ ...view, managed: { keepInventory: true } })).toBe("pack");
    expect(keepMode(mcRules("slot2", true))).toBe("off");
  });

  it("the saved choice underneath is the slot's rule, else Off", () => {
    expect(keepSaved(castle)).toBe(false);
    expect(keepSaved({ ...castle, rules: { keepInventory: true } })).toBe(true);
    expect(keepMeaning("pack", true)).toBe(
      "Underneath it's On — that comes back if Power Pack is turned off.",
    );
  });

  it("the help line states the mode in plain words", () => {
    expect(KEEP_HELP).toMatch(/only while carrying a Power Pack and their own Tricorder/);
    expect(KEEP_HELP).toMatch(/one pack burns each time/);
    expect(KEEP_HELP).toMatch(/Without both, they drop everything/);
    expect(KEEP_HELP).not.toMatch(/vanilla|keepInventory/i);
  });

  it("leaving Power Pack turns the switch off first, and sets the rule only if it differs", () => {
    expect(keepSteps("off", "on", false).map((s) => [s.kind, s.on])).toEqual([["rule", true]]);
    expect(keepSteps("on", "pack", true).map((s) => [s.kind, s.on])).toEqual([["pack", true]]);
    expect(keepSteps("pack", "on", true).map((s) => [s.kind, s.on])).toEqual([["pack", false]]);
    const two = keepSteps("pack", "off", true);
    expect(two.map((s) => [s.kind, s.on])).toEqual([
      ["pack", false],
      ["rule", false],
    ]);
    expect(two[0]?.msg).toBe("Turning Power Pack off — keep inventory goes back to your saved On…");
    expect(keepSteps("on", "on", true)).toEqual([]);
  });

  it("words the sidecar's refusals plainly", () => {
    expect(refusalReason("busy: restoring a backup")).toBe("the server is restoring a backup");
    expect(refusalReason("busy: restart")).toBe("the server is restarting");
    expect(refusalReason("busy: another action")).toBe("the server is busy with another action");
    expect(refusalReason("server is not running")).toBe("the server isn't running");
    expect(refusalReason("keepInventory is managed by the Power Pack charm while it is on")).toBe(
      "Power Pack is holding it on",
    );
    expect(refusalReason("something else.")).toBe("something else");
  });

  it("says nothing changed only for a refusal the re-read confirms", () => {
    const base = { to: "pack" as const, from: "off" as const, reason: "the server is updating" };
    expect(keepFailText({ ...base, now: "off", refused: true })).toBe(
      "Couldn't set keep inventory to Power Pack — the server is updating. Nothing changed.",
    );
    expect(keepFailText({ ...base, now: "off", refused: false })).not.toMatch(/Nothing changed/);
    expect(keepFailText({ ...base, now: null, refused: true })).toMatch(
      /Couldn't read it back\. The server may still be working on it\.$/,
    );
    expect(keepFailText({ ...base, now: "pack", refused: true })).toMatch(
      /^Keep inventory reads Power Pack now, but the server reported a problem/,
    );
    expect(keepFailText({ to: "off", from: "pack", now: "on", refused: true, reason: "x" })).toBe(
      "Couldn't set keep inventory to Off — x. Power Pack is off and it reads On for now.",
    );
  });

  it("the toast says live, at the start, or at the next load from the slot", () => {
    expect(keepSavedToast("pack", "off", castle, true)).toBe(
      "Applied live — keep inventory Power Pack",
    );
    expect(keepSavedToast("pack", "off", castle, false)).toBe(
      "Saved — keep inventory Power Pack when the server starts",
    );
    expect(keepSavedToast("off", "pack", sky, false)).toBe(
      "Saved — Power Pack off, keep inventory Off from Skyblock run's next load",
    );
  });
});
