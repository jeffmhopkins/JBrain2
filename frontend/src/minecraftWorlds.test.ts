import { describe, expect, it } from "vitest";
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
  jobLine,
  loadConfirm,
  nextToGo,
  resetConfirm,
  resetOptions,
  restoreConfirm,
  restoreTargetText,
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
    const spec = importOverConfirm(castle, "Hilltop Village", running);
    expect(spec.tone).toBe("danger");
    expect(spec.body).toBe(
      "Castle Hill is backed up first, then replaced by Hilltop Village with the file's own settings — BlockyFox and Mira_P get a 10-second warning in chat and are disconnected for about a minute.",
    );
    expect(importOverConfirm(creative, "Hilltop Village", running).tone).toBe("warn");
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
});
