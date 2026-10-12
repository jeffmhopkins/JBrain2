// node --test: Dave's welcome and the first-time beats (plan §DW1) — welcome.js through
// main.js against the fake server, and welcome_rules.js. Laid out and run by the
// supervisor's test_minecraft_jbrain_pack.py.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { ItemStack, sim } from "@minecraft/server";
import { LINES } from "./pack/scripts/rules.js";
import { firstDeathLines, helloTimeline, isHello, parseBeats, parseWreck, powerPackLines,
  welcomeTimeline } from "./pack/scripts/welcome_rules.js";

await import("./pack/scripts/main.js");
sim.startup();

const DAVE = "§b[Dave]§r ";
const WELCOME_TICKS = 500;
const tricorders = (p) => p.inventory.slots.filter((s) => s?.typeId === "jbrain:tricorder");
const daveEvents = () => sim.logs.filter((l) => l.includes('"ev":"dave"'));
const beats = (p) => JSON.parse(sim.props.get(`jb:beats:${p.id}`) ?? "[]");
const said = (p) => p.messages.filter((m) => m.startsWith(DAVE)).map((m) => m.slice(DAVE.length));
const ask = (p, name, ...words) => {
  sim.command(name).cb({ sourceEntity: p }, ...words);
  sim.flush();
};
const wake = (p) => {
  sim.spawn(p, true);
  sim.advance(WELCOME_TICKS);
  p.messages.length = 0;
  p.sounds.length = 0;
};
const config = (cfg) => sim.scriptevent("jb:config", JSON.stringify(cfg));
const look = () => {
  for (let i = 0; i < 4; i++) sim.tick(); // the loop looks at what they carry once a second
};

beforeEach(() => sim.reset());

test("a new player's welcome follows the lore table, private to them", () => {
  const p = sim.player("BlockyFox", "-1");
  p.selectedSlotIndex = 4;
  sim.spawn(p, true);
  // 0 s: dark for 2 s, and the first actionbar.
  assert.equal(p.fades.length, 1);
  assert.deepEqual(p.fades[0].fadeColor, { red: 0, green: 0, blue: 0 });
  assert.equal(p.fades[0].fadeTime.holdTime, 2);
  assert.equal(p.actionbar.at(-1), `§7§o${LINES.whereAmI}`);
  assert.equal(tricorders(p).length, 0);
  assert.deepEqual(beats(p), ["welcome"]); // recorded as it starts

  sim.advance(50); // 2.5 s: sparkles start falling, with the chime
  sim.advance(40);
  assert.equal(p.particles.length, 10);
  assert.ok(p.particles.every((s) => s.id === "minecraft:endrod"));
  const ys = p.particles.map((s) => s.loc.y);
  assert.deepEqual(ys, [...ys].sort((a, b) => b - a)); // falling onto them
  assert.ok(p.sounds.includes("note.chime"));
  assert.equal(p.actionbar.at(-1), `§7§o${LINES.whatsThis}`); // 4.5 s

  sim.advance(9);
  assert.equal(tricorders(p).length, 0);
  sim.advance(1); // 5 s: the Tricorder, in their hand
  assert.equal(p.inventory.slots[4]?.nameTag, "BlockyFox's Tricorder");
  assert.equal(p.selectedSlotIndex, 4);

  sim.advance(20); // 6 s: the signal
  assert.deepEqual(p.titles, [
    { title: `§b${LINES.signal}`, subtitle: `§7§o${LINES.signalSub}`, fadeInDuration: 10, stayDuration: 40, fadeOutDuration: 10 },
  ]);
  assert.ok(p.sounds.includes("random.fizz"));

  sim.advance(39);
  assert.deepEqual(said(p), []);
  sim.advance(1); // 8 s: Dave's first line, then one every 3 s
  assert.deepEqual(said(p), [LINES.welcome("BlockyFox", false)[0]]);
  sim.advance(300);
  assert.deepEqual(said(p), LINES.welcome("BlockyFox", false)); // a world never told: no wreck
  assert.equal(p.messages.length, 6);
  assert.equal(daveEvents().length, 0); // scripted, never an AI turn
});

test("a crash-site world swaps in the pod and ship lines", () => {
  config({ wreck: true });
  assert.ok(sim.logs.includes("[jbrain] config wreck=true"));
  const p = sim.player("Mira_P", "-2");
  sim.spawn(p, true);
  sim.advance(WELCOME_TICKS);
  const lines = said(p);
  assert.deepEqual(lines, LINES.welcome("Mira_P", true));
  assert.match(lines[2], /^Look up\./);
  assert.match(lines[3], /^Your pod kept you alive\./);

  config({ wreck: false });
  const q = sim.player("Other", "-3");
  sim.spawn(q, true);
  sim.advance(WELCOME_TICKS);
  assert.match(said(q)[2], /^Our ship came down long ago\./);
});

test("a player from before DW1 gets the welcome once, keeping their own Tricorder", () => {
  const p = sim.player("Old Hand", "-4");
  wake(p);
  // Before DW1: their Tricorder in the hotbar's slot 6, and no beat recorded.
  const own = tricorders(p)[0];
  p.inventory.slots.fill(undefined);
  p.inventory.setItem(6, own);
  p.selectedSlotIndex = 0;
  sim.props.delete("jb:beats:-4");
  const serial = sim.props.get("jb:tricorder:-4");

  sim.spawn(p, true);
  sim.advance(WELCOME_TICKS);
  assert.equal(tricorders(p).length, 1);
  assert.equal(sim.props.get("jb:tricorder:-4"), serial); // never re-beamed
  assert.equal(p.selectedSlotIndex, 6); // their own, into their hand
  assert.deepEqual(said(p), LINES.welcome("Old Hand", false));

  // The next join: no replay and no safety-net line.
  p.messages.length = 0;
  sim.spawn(p, true);
  sim.advance(WELCOME_TICKS);
  assert.deepEqual(p.messages, []);
  assert.equal(p.fades.length, 2); // only the two welcomes' darkness
});

test("leaving mid-welcome ends it quietly; rejoining doesn't replay it", () => {
  const p = sim.player("Leaver", "-5");
  sim.spawn(p, true);
  sim.advance(40);
  p.isValid = false; // gone before the beam
  sim.advance(WELCOME_TICKS);
  assert.deepEqual(p.messages, []);
  assert.equal(tricorders(p).length, 0);
  assert.equal(p.titles.length, 0);

  const back = sim.player("Leaver", "-5"); // a new entity on rejoin
  sim.spawn(back, true);
  assert.equal(back.fades.length, 0);
  assert.equal(tricorders(back).length, 1); // the Tricorder they never got, quietly
  sim.advance(WELCOME_TICKS);
  assert.deepEqual(back.messages, []);

  const late = sim.player("Late", "-6");
  sim.spawn(late, true);
  sim.advance(220); // two of Dave's lines in (8 s and 11 s)
  late.isValid = false;
  sim.advance(WELCOME_TICKS);
  assert.equal(said(late).length, 2);
});

test("a respawn during the welcome doesn't fire the safety net; later wakings still do", () => {
  const p = sim.player("Quick", "-7");
  sim.spawn(p, true);
  sim.advance(40);
  sim.spawn(p, false); // died before the Tricorder came down
  assert.equal(tricorders(p).length, 0);
  sim.advance(WELCOME_TICKS);
  assert.equal(tricorders(p).length, 1);
  assert.ok(!p.messages.includes(DAVE + LINES.rebeamed));

  p.messages.length = 0;
  p.inventory.slots.fill(undefined); // a /clear got past the lock
  sim.spawn(p, false);
  assert.equal(tricorders(p).length, 1);
  assert.deepEqual(p.messages, [DAVE + LINES.rebeamed]);
});

test("first death: Dave's two lines, once", () => {
  const p = sim.player("Faller", "-8");
  wake(p);
  sim.die(p);
  sim.spawn(p, false);
  sim.advance(100);
  assert.deepEqual(said(p), LINES.firstDeath);
  assert.ok(beats(p).includes("first_death"));
  sim.die(p);
  sim.spawn(p, false);
  sim.advance(100);
  assert.equal(said(p).length, 2);
});

test("first death saved by a Power Pack: only the first line, never 'where you fell'", () => {
  config({ power_pack_keep: true });
  sim.rules.keepInventory = true;
  const p = sim.player("Saved", "-9");
  wake(p);
  p.inventory.setItem(3, new ItemStack("jbrain:power_pack", 2));
  sim.die(p);
  sim.spawn(p, false);
  sim.advance(100);
  assert.deepEqual(p.messages, ["§7" + LINES.burnt(1), DAVE + LINES.firstDeath[0]]);
  assert.ok(beats(p).includes("first_death"));
  sim.die(p);
  sim.spawn(p, false);
  sim.advance(100);
  assert.equal(said(p).length, 1); // the beat is spent
});

test("first Ender Pearl: Dave's line the moment the unlock is recorded, once", () => {
  const p = sim.player("Pearl", "-10");
  wake(p);
  p.inventory.setItem(4, new ItemStack("minecraft:ender_pearl", 1));
  look();
  assert.deepEqual(said(p), [LINES.firstEnderPearl]);
  assert.ok(beats(p).includes("first_ender_pearl"));
  look();
  p.inventory.setItem(4, undefined);
  look();
  p.inventory.setItem(5, new ItemStack("minecraft:ender_pearl", 2));
  look();
  assert.equal(said(p).length, 1);
});

test("first Replicator: Dave's line once a player holds 8 Power Packs, once", () => {
  const p = sim.player("Maker", "-12");
  wake(p);
  p.inventory.setItem(4, new ItemStack("jbrain:power_pack", 8));
  look(); // replicator.js looks once a second
  assert.ok(said(p).includes(LINES.firstReplicator));
  assert.ok(beats(p).includes("first_replicator"));
  const heard = said(p).length;
  p.inventory.setItem(4, undefined);
  look();
  p.inventory.setItem(4, new ItemStack("jbrain:power_pack", 8));
  look();
  assert.equal(said(p).length, heard);
});

for (const [keep, transport, want] of [
  [true, true, [LINES.firstPowerPack.keep, LINES.firstPowerPack.transport]],
  [true, false, [LINES.firstPowerPack.keep]],
  [false, true, [LINES.firstPowerPack.transport]],
]) {
  test(`first Power Pack, keep ${keep} and transport ${transport}: only what's built`, () => {
    config({ power_pack_keep: keep, power_pack_transport: transport });
    const p = sim.player("Packer", "-11");
    wake(p);
    p.inventory.setItem(4, new ItemStack("jbrain:power_pack", 1));
    look();
    assert.deepEqual(said(p), want);
    assert.ok(beats(p).includes("first_power_pack"));
    look();
    assert.equal(said(p).length, want.length);
  });
}

test("first Power Pack with neither switch on: nothing said, nothing recorded, heard later", () => {
  config({ power_pack_keep: false, power_pack_transport: false });
  const p = sim.player("Early", "-12");
  wake(p);
  p.inventory.setItem(4, new ItemStack("jbrain:power_pack", 1));
  look();
  assert.deepEqual(p.messages, []);
  assert.ok(!beats(p).includes("first_power_pack"));
  assert.ok(sim.props.get("jb:known:-12").includes("power_pack_uses"));

  config({ power_pack_transport: true }); // the owner turns one on
  look();
  assert.deepEqual(said(p), [LINES.firstPowerPack.transport]);
  assert.ok(beats(p).includes("first_power_pack"));
});

test("/dave hello replays the welcome's lines, no darkness, no Tricorder, no backend", () => {
  config({ wreck: true });
  const p = sim.player("Hi", "-13");
  wake(p);
  const fades = p.fades.length;
  ask(p, "jb:dave", "hello");
  assert.deepEqual(said(p), [LINES.welcome("Hi", true)[0]]); // at once
  sim.advance(59);
  assert.equal(said(p).length, 1);
  sim.advance(1);
  assert.equal(said(p).length, 2);
  ask(p, "jb:dave", "hello"); // asked again mid-telling: one telling
  sim.advance(WELCOME_TICKS);
  assert.deepEqual(said(p), LINES.welcome("Hi", true));
  assert.equal(tricorders(p).length, 1);
  assert.equal(p.fades.length, fades);
  assert.equal(p.titles.length, 1); // only the welcome's
  assert.equal(daveEvents().length, 0);

  p.messages.length = 0;
  ask(p, "jb:d", "Hello");
  sim.advance(WELCOME_TICKS);
  assert.deepEqual(said(p), LINES.welcome("Hi", true));
  assert.equal(daveEvents().length, 0);

  ask(p, "jb:dave", "hello", "there"); // a real question still goes to Dave
  assert.equal(daveEvents().length, 1);
});

test("/dave hello still needs your own Tricorder", () => {
  const p = sim.player("Bare", "-14");
  ask(p, "jb:ai", "hello");
  sim.advance(WELCOME_TICKS);
  assert.deepEqual(p.messages, ["§7§o" + LINES.noSignal]);
  assert.equal(daveEvents().length, 0);
});

test("the rules: timeline, hello, beats, death and pack lines, the wreck flag", () => {
  const t = welcomeTimeline("A", false);
  assert.deepEqual(t.slice(0, 5).map((s) => [s.at, s.step]), [
    [0, "dark"], [50, "sparkles"], [90, "whatsThis"], [100, "tricorder"], [120, "signal"],
  ]);
  assert.deepEqual(t.slice(5).map((s) => s.at), [160, 220, 280, 340, 400, 460]); // 8 s to 23 s
  assert.deepEqual(helloTimeline("A", true).map((s) => s.at), [0, 60, 120, 180, 240, 300]);
  assert.match(LINES.welcome("BlockyFox", false)[0], /there you are, BlockyFox\./);
  assert.match(LINES.welcome("x", true).at(-1), /Type \/dave and ask/);
  for (const yes of ["hello", " Hello ", "HELLO"]) assert.ok(isHello(yes));
  for (const no of ["hello there", "hi", "hello?", ""]) assert.ok(!isHello(no));
  assert.deepEqual(parseBeats('["welcome", 3]'), ["welcome"]);
  assert.deepEqual(parseBeats("nope"), []);
  assert.deepEqual(parseBeats(undefined), []);
  assert.deepEqual(firstDeathLines(true), [LINES.firstDeath[0]]);
  assert.deepEqual(firstDeathLines(false), LINES.firstDeath);
  assert.deepEqual(powerPackLines({ keep: false, transport: false }), []);
  assert.deepEqual(parseWreck('{"wreck": true, "power_pack_keep": false}'), { wreck: true });
  assert.deepEqual(parseWreck('{"wreck": "yes"}'), {});
  assert.throws(() => parseWreck("null"));
});
