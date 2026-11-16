// GrowBot body test: a guided, scripted trial of the real robot, logged to the box.
//
// WHY THIS EXISTS (owner, 2026-11-15): "a button that says body test, and when I press it, it'll
// tell me what to do: place this phone on the body, power on the body, stand 1 m away and say
// '1 meter', and then it starts the test ... and log everything so you can review after". The
// Body tab sends it (a skill set's look with follow "test"); this runs on the creature's phone,
// which is the one on the body and the one with the camera, the legs and the ears. It walks the
// person through setup (place, power, stand up, measure 1 m), then runs the bench checks the
// follow-me trial still owes (docs/plans/GROWBOT_POLICY_DRIVE_PLAN.md "Known gaps"): which way
// the frames run, which way a positive pitch tilts the camera, look and hold, 90-degree turns,
// creep while holding after them, a short walk each way, and whether "stop stop" is heard over
// the legs. Every sample (about 20 a second), event, command and fall goes to the box in chunks
// (`/api/box/bodytest`), so a run can be read back afterwards from the debug API.
//
// The page glue (camera, legs, gyro, screen) is passed in as `env` by growbot-box.js
// (gbBodyTest), so the whole script runs in Node against a fake env and a fake clock.
//
// No `??` or `?.`: the creature's page runs on old iPhones (growbot_runner.js).

export var SETUP = [
  { id: "place", title: "Place this phone on the body", sub: "Screen facing out, front camera at the top. Snap it into the cradle." },
  { id: "power", title: "Power on the body", sub: "Flip the switch. This moves on by itself once the legs answer." },
  { id: "stand", title: "Say “stand up”", sub: "It gets up if it can, then stands tall so the camera sees you. If it can't, stand it up by hand." },
  { id: "measure", title: "Stand 1 m away and say “1 meter”", sub: "Face the screen. I'll measure you, then the test starts by itself." },
];

export var CHECKS = [
  { id: "leftright", name: "Left-right check (legs still)" },
  { id: "tilt", name: "Camera tilt check" },
  { id: "hold", name: "Look and hold, 10 s" },
  { id: "turn", name: "Turn left 90°, right 90°" },
  { id: "creep", name: "Hold still: creep after spins" },
  { id: "forward", name: "Walk forward" },
  { id: "back", name: "Walk back" },
  { id: "estop", name: "“stop stop” while walking" },
];

export var T = {
  sampleMs: 50,          // telemetry rate: 20 a second
  steerMs: 200,          // the Drive's steer holds 1.5 s, so a few a second keeps a walk going
  flushMs: 2000,         // a chunk to the box this often
  rowsMax: 400,          // and sooner when this many rows wait
  faceFreshMs: 600,      // a face older than this is not "seen now"
  uprightHoldMs: 1000,   // standing up counts once upright this long
  standWaitMs: 15000,
  settleMs: 2500,        // each walk's own stance (1 s) and the selector's 1.5 s hold
  turnMs: 8000,
  holdMs: 10000,
  creepMs: 15000,
  walkMs: 4000,
  estopMs: 10000,
  setupMaxMs: 600000,    // a test left in setup this long ends, rather than log forever
  coolMs: 4500,          // a fall's cool-off (growbot-box.js _gaitCoolUntil, 4 s) before a walk
  nearStop: 0.45,        // a forward walk ends early this close to the person (m)
};

var WORDS = {
  ready: /\b(ready|it'?s on|done)\b/i,
  stand: /\bstand(ing)?\s*up\b/i,
  metre: /\b(1|one|a)[\s-]*(m|meter|metre|meters|metres)\b/i,
  cancel: /\b(cancel|end|stop|quit)\s+(the\s+)?test\b/i,
  stop: /\bstop\b/i,
  stop2: /\bstop\b[\s,.!?;:\u2026\u2013\u2014-]*\bstop\b/i,
  // Whisper's stock lines for silence (next.html heardText drops these too, after this hook).
  silence: /^[\s.!?]*(you|thank you|thanks for watching|thank you for watching)?[\s.!?]*$/i,
};

// What a heard line means to the test, or "" (the line itself is never logged unless it's one).
export function heardWord(txt) {
  var t = String(txt || "");
  if (WORDS.cancel.test(t)) return "cancel";
  if (WORDS.metre.test(t)) return "metre";
  if (WORDS.stand.test(t)) return "stand";
  if (WORDS.stop.test(t)) return "stop";
  if (WORDS.ready.test(t)) return "ready";
  return "";
}

function r1(v) { return typeof v === "number" && isFinite(v) ? Math.round(v * 10) / 10 : null; }
function r2(v) { return typeof v === "number" && isFinite(v) ? Math.round(v * 100) / 100 : null; }
function r3(v) { return typeof v === "number" && isFinite(v) ? Math.round(v * 1000) / 1000 : null; }

export function mean(xs) {
  var s = 0, n = 0;
  for (var i = 0; i < xs.length; i++) if (typeof xs[i] === "number" && isFinite(xs[i])) { s += xs[i]; n++; }
  return n ? s / n : null;
}

// ---- the verdicts: pure, so the tests pin each threshold ----------------------------------

// The person stepped to THEIR right, which is the robot's left: a positive bearing when the
// frames run as growbot-follow.js assumes. A negative move says the frames are reversed.
export function judgeLeftRight(before, after) {
  if (before === null || after === null) return { v: "warn", why: "couldn't see your face both times", delta: null };
  var d = after - before;
  if (d > 8) return { v: "ok", why: "you stepped right, bearing went +" + Math.round(d) + "°; frames run the right way", delta: r1(d) };
  if (d < -8) return { v: "warn", why: "you stepped right, bearing went " + Math.round(d) + "°; frames were reversed, so I flipped them", delta: r1(d), flip: true };
  return { v: "warn", why: "bearing moved only " + Math.round(d) + "°; did you step about half a metre?", delta: r1(d) };
}

// The face sits above centre (a standing person over a low camera). Raising the camera toward it
// moves it down the frame, so its `up` angle falls from pitch -1 to pitch +1.
export function judgeTilt(upLow, upHigh, betaLow, betaHigh) {
  var tilt = betaLow !== null && betaHigh !== null ? r1(betaHigh - betaLow) : null;
  if (upLow === null || upHigh === null) return { v: "warn", why: "couldn't see your face at both heights", tilt: tilt };
  var d = upHigh - upLow;
  var phone = tilt !== null ? "; phone tilted " + (tilt > 0 ? "+" : "") + tilt + "°" : "";
  if (d < -2) return { v: "ok", why: "+pitch moved your face " + Math.round(-d) + "° toward centre" + phone, shift: r1(d), tilt: tilt };
  if (d > 2) return { v: "warn", why: "+pitch moved your face " + Math.round(d) + "° away from centre: tilt reversed?" + phone, shift: r1(d), tilt: tilt, reversed: true };
  return { v: "warn", why: "the camera barely moved between low and tall" + phone, shift: r1(d), tilt: tilt };
}

// One leg of a turn: samples [{t, h}] from its start, against its target.
// `target` is the walk's own heading (deg from where it started); `reached` is how far this leg
// turned, measured from its first sample, at its closest to the target.
export function turnLeg(samples, target, t0) {
  var best = null, at = null, start = null;
  for (var i = 0; i < samples.length; i++) {
    var s = samples[i];
    if (typeof s.h !== "number") continue;
    if (start === null) start = s.h;
    if (best === null || Math.abs(target - s.h) < Math.abs(target - best)) best = s.h;
    if (at === null && Math.abs(target - s.h) <= 15) at = s.t - t0;
  }
  return { reached: best === null ? null : r1(best - start), secs: at === null ? null : r1(at / 1000) };
}

export function judgeTurn(left, right) {
  var ok = function (l) { return l.secs !== null; };
  var say = function (l, sign) { return (l.reached === null ? "?" : (l.reached > 0 ? "+" : "") + Math.round(l.reached)) + "°" + (l.secs !== null ? " in " + l.secs + " s" : " (never within 15° of " + sign + "90)"); };
  var v = ok(left) && ok(right) ? "ok" : (left.reached === null && right.reached === null ? "fail" : "warn");
  return { v: v, why: "left " + say(left, "+") + ", right " + say(right, "-"), left: left, right: right };
}

// Holding still after the spins: how far it slid (by the face's distance) and turned (gyro).
export function judgeCreep(d0, d1, h0, h1, secs) {
  var cm = d0 !== null && d1 !== null ? Math.round((d1 - d0) * 100) : null;
  var deg = h0 !== null && h1 !== null ? r1(h1 - h0) : null;
  var bits = [];
  if (cm !== null) bits.push((cm > 0 ? "+" : "") + cm + " cm");
  if (deg !== null) bits.push((deg > 0 ? "+" : "") + deg + "°");
  var why = (bits.length ? "drifted " + bits.join(" and ") : "couldn't measure") + " in " + secs + " s of hold";
  var bad = (cm !== null && Math.abs(cm) > 5) || (deg !== null && Math.abs(deg) > 10);
  return { v: bits.length ? (bad ? "warn" : "ok") : "warn", why: why, cm: cm, deg: deg };
}

// A walk toward the person (dir +1) closes the distance; the nose walk (-1) opens it.
export function judgeWalk(d0, d1, dir) {
  if (d0 === null || d1 === null) return { v: "warn", why: "couldn't see your face before and after", moved: null };
  var moved = d0 - d1; // + is closer to the person
  var cm = Math.round(moved * 100);
  var why = (dir > 0 ? "came " : "backed ") + Math.abs(cm) + " cm" + ((dir > 0) === (moved > 0) ? "" : " the wrong way") + " by face distance";
  if (dir * moved > 0.03) return { v: "ok", why: why, moved: r2(moved) };
  return { v: dir * moved < -0.03 ? "fail" : "warn", why: Math.abs(cm) < 3 ? "barely moved (" + cm + " cm) by face distance" : why, moved: r2(moved) };
}

export function judgeEstop(stoppedMs, heard, drops) {
  if (stoppedMs !== null) return { v: "ok", why: "stopped " + r1(stoppedMs / 1000) + " s after the ask", ms: stoppedMs };
  var why = heard > 0 ? "heard " + heard + " line(s) but no “stop stop”" :
    drops > 0 ? drops + " clip(s) dropped as its own noise; never transcribed" : "nothing was heard";
  return { v: "fail", why: why + "; stopped by the time limit", ms: null, drops: drops };
}

export function judgeFalls(n) {
  return n ? { v: "fail", why: n + " fall(s) or grab(s); see the log", n: n } : { v: "ok", why: "none", n: 0 };
}

// ---- the run ---------------------------------------------------------------------------------

function Cancelled(why) { this.why = why; }
function Fell(why) { this.why = why; }

export function BodyTest(env, skills, opts) {
  this.env = env;
  this.skills = skills;
  this.opts = opts || {};
  this.run_id = env.newId();
  this.t0 = env.now();
  this.seq = 0;
  this.rows = [];
  this.lastFlush = this.t0;
  this.posting = Promise.resolve();
  this.dead = null;
  this.waiting = null;     // the word or tap a setup step waits for
  this.got = null;
  this.phase = "setup";
  this.setupIdx = 0;
  this.checkIdx = -1;
  this.ask = "";
  this.results = {};
  this.falls = 0;
  this.fell = null;
  this.drops = 0;
  this.heardN = 0;
  this.estopAt = null;
  this.expectStop = false;
  this.drive = "";
  this.walking = false;
  this.status = "running";
  this.say = "";
}

BodyTest.prototype.elapsed = function () { return this.env.now() - this.t0; };

BodyTest.prototype.log = function (k, d) {
  var row = { t: this.env.now(), k: k };
  for (var key in d) if (Object.prototype.hasOwnProperty.call(d, key)) row[key] = d[key];
  this.rows.push(row);
  return row;
};

BodyTest.prototype.stepName = function () {
  if (this.phase === "setup") return SETUP[this.setupIdx].id;
  if (this.phase === "run" && this.checkIdx >= 0) return CHECKS[this.checkIdx].id;
  return this.phase;
};

// What the box (and so the Body tab's mirror) shows as this run's latest state.
BodyTest.prototype.summary = function () {
  var tl = this.lastTele || {}, f = this.lastFace;
  return {
    phase: this.phase,
    step: this.stepName(),
    step_n: this.phase === "run" ? this.checkIdx + 1 : 0,
    steps: CHECKS.length,
    label: this.phase === "setup" ? SETUP[this.setupIdx].title : this.phase === "run" && this.checkIdx >= 0 ? CHECKS[this.checkIdx].name : this.phase,
    ask: this.ask,
    secs: Math.round(this.elapsed() / 1000),
    mode: tl.mode || "",
    heading: r1(tl.h),
    face: f ? { dist: r2(f.dist), bearing: r1(f.bearing) } : null,
    falls: this.falls,
    results: this.results,
  };
};

BodyTest.prototype.flush = function (final) {
  if (!final && !this.rows.length) return this.posting;
  var rows = this.rows;
  this.rows = [];
  this.lastFlush = this.env.now();
  var chunk = {
    run: this.run_id, seq: this.seq++, status: final ? this.status : "running",
    skills: this.seq === 1 ? this.skills : undefined, summary: this.summary(), rows: rows,
  };
  var env = this.env;
  // In order, one at a time; a chunk the box missed is lost, never retried into a pile-up.
  this.posting = this.posting.then(function () { return env.post(chunk); }).catch(function () {});
  return this.posting;
};

BodyTest.prototype.sample = function () {
  var env = this.env, tl = env.tele() || {}, f = env.face();
  this.lastTele = tl;
  this.lastFace = f && env.now() - f.t <= T.faceFreshMs ? f : null;
  var row = { s: this.stepName() };
  for (var k in tl) if (Object.prototype.hasOwnProperty.call(tl, k)) row[k] = tl[k];
  if (this.lastFace) {
    row.fb = r1(f.bearing); row.fu = r1(f.up); row.fd = r3(f.dist); row.fn = f.n || 1;
    if (f.box) row.box = [r3(f.box.x), r3(f.box.y), r3(f.box.width), r3(f.box.height)];
  }
  var kept = this.log("s", row);
  // The running check's own samples, which its verdict reads.
  if (this.mine) this.mine.push(kept);
  if (this.rows.length >= T.rowsMax || env.now() - this.lastFlush >= T.flushMs) this.flush(false);
  env.show(this.view());
};

// Waits `ms`, sampling throughout; `tick` (if given) runs each sample and may end the wait by
// returning true. A cancel or a fall ends it by throwing.
BodyTest.prototype.wait = async function (ms, tick) {
  var end = this.env.now() + ms, lastSteer = -Infinity;
  for (;;) {
    if (this.phase === "setup" && this.elapsed() > T.setupMaxMs) this.dead = "setup_timeout";
    if (this.dead) throw new Cancelled(this.dead);
    if (this.fell) { var why = this.fell; this.fell = null; throw new Fell(why); }
    this.sample();
    if (this.walking && this.env.now() - lastSteer >= T.steerMs) {
      lastSteer = this.env.now();
      if (!this.env.steer(this.drive, this.cmd.heading, this.cmd.speed, this.cmd.pitch)) {
        this.walking = false;
        if (this.expectStop) { if (this.estopAt === null) this.estopAt = this.env.now(); return true; }
        if (this.fell) continue;
        throw new Fell("walk_ended");
      }
    }
    if (tick && tick.call(this)) return true;
    if (this.env.now() >= end) return false;
    await this.env.sleep(T.sampleMs);
  }
};

// Waits for a setup step's word or tap (any of `words`), or for `until` to say it's done.
BodyTest.prototype.waitFor = async function (words, until) {
  this.waiting = words;
  this.got = null;
  try {
    for (;;) {
      if (await this.wait(3600000, function () { return !!this.got || (until ? until.call(this) : false); })) {
        var got = this.got || "auto";
        this.got = null;
        return got;
      }
    }
  } finally { this.waiting = null; }
};

BodyTest.prototype.walk = async function (cmd) {
  this.cmd = { heading: cmd.heading || 0, speed: cmd.speed || 0, pitch: typeof cmd.pitch === "number" ? cmd.pitch : 0 };
  if (this.walking) return;
  this.drive = this.env.newDrive();
  var why = this.env.walk(this.skills, this.drive);
  if (why) throw new Fell("walk_refused: " + why);
  this.walking = true;
  this.log("e", { e: "bt_walk", drive: this.drive });
  // Its stance (1 s), then the selector holds a skill 1.5 s before anything else may start.
  await this.wait(T.settleMs);
};

BodyTest.prototype.steer = function (cmd) {
  for (var k in cmd) if (Object.prototype.hasOwnProperty.call(cmd, k)) this.cmd[k] = cmd[k];
  this.log("e", { e: "bt_cmd", h: this.cmd.heading, sp: this.cmd.speed, p: this.cmd.pitch });
  if (this.walking) this.env.steer(this.drive, this.cmd.heading, this.cmd.speed, this.cmd.pitch);
};

BodyTest.prototype.legsOff = function () {
  if (this.walking) { this.walking = false; this.env.legsOff(); }
};

// Averages a field over the samples logged in the last `ms`.
BodyTest.prototype.recent = function (key, ms) {
  var since = this.env.now() - ms, xs = [], all = this.mine;
  for (var i = all.length - 1; i >= 0 && all[i].t >= since; i--) xs.push(all[i][key]);
  return mean(xs);
};

BodyTest.prototype.setAsk = function (words) {
  this.ask = words;
  this.log("e", { e: "bt_ask", ask: words });
  this.env.show(this.view());
};

// ---- setup -----------------------------------------------------------------------------------

BodyTest.prototype.setup = async function () {
  var e = this.env;
  this.setupIdx = 0; this.mark("place");
  this.say = "";
  await this.waitFor(["ready", "tap"]);

  this.setupIdx = 1; this.mark("power");
  // A tap only moves on once the legs answer (2026-11-16 run #1: a tap skipped this step with the
  // body off, and "stand up" then had nothing to move).
  this.say = "";
  while (!e.bodyUp()) {
    await this.waitFor(["tap"], function () { return e.bodyUp(); });
    if (!e.bodyUp()) this.say = "The legs aren't answering yet. Check the switch and the battery.";
  }

  this.setupIdx = 2; this.mark("stand");
  // Standing counts once it has been upright uprightHoldMs, however it got there: by itself, or
  // stood up by hand when it can't get up on its own (2026-11-16: the get-up wasn't set up on this
  // body, so every "stand up" was refused and nothing moved).
  var upSince = null;
  var held = function () {
    if (e.upright() === true) { if (upSince === null) upSince = e.now(); return e.now() - upSince >= T.uprightHoldMs; }
    upSince = null;
    return false;
  };
  this.say = "";
  // Lying down at the start: stood up by hand counts too. Already up: it waits for the word, so
  // the tall stance lifts the camera.
  var down = e.upright() !== true;
  for (;;) {
    var how = await this.waitFor(["stand", "tap"], down ? held : null);
    if (how === "auto") { this.log("e", { e: "bt_stand", ok: true, how: "by_hand" }); break; }
    // Already being stood up by hand: let the hold finish rather than move the legs under the hands.
    if (down && e.upright() === true) continue;
    var did = e.standUp();
    this.say = did === "getup" ? "Getting up…" : did === "tall" ? "Standing tall…"
      : "I can't get up on my own yet. Stand me up by hand and I'll carry on.";
    upSince = null;
    var stood = did ? await this.wait(T.standWaitMs, held) : false;
    this.log("e", { e: "bt_stand", ok: stood, how: did || "none" });
    if (stood) break;
    down = true;
    if (did) this.say = "I couldn't get upright. Stand me up by hand and I'll carry on, or say “stand up” to try again.";
  }

  this.setupIdx = 3; this.mark("measure");
  e.camFront();
  for (;;) {
    this.say = "";
    await this.waitFor(["metre", "tap"]);
    try {
      var k = await e.calibrate();
      this.log("e", { e: "bt_calibrate", scale: r3(k) });
      this.say = "Measured. Starting in 3…";
      break;
    } catch (err) {
      this.say = "I can't see your face. Face the screen and say “1 meter” again.";
      this.log("e", { e: "bt_calibrate", fail: String((err && err.message) || err).slice(0, 80) });
    }
  }
  for (var n = 3; n >= 1; n--) { this.say = "Starting in " + n + "…"; await this.wait(1000); }
};

BodyTest.prototype.mark = function (step) {
  this.log("e", { e: "bt_step", step: step });
  var still = this.env.still();
  if (still) this.log("still", { step: step, jpg: still });
};

// ---- the checks ------------------------------------------------------------------------------

BodyTest.prototype.check = async function (i, fn) {
  this.checkIdx = i;
  this.mark(CHECKS[i].id);
  this.mine = [];
  var res;
  try {
    res = await fn.call(this);
    // Each check starts its own walk, so each one's heading starts at 0 and none outlasts the
    // Drive's 2-minute cap.
    this.legsOff();
  } catch (err) {
    if (err instanceof Cancelled) throw err;
    if (!(err instanceof Fell)) throw err;
    this.legsOff();
    res = { v: "fail", why: err.why === "walk_ended" ? "the walk ended early" : err.why };
    this.results[CHECKS[i].id] = res;
    this.log("e", { e: "bt_check", id: CHECKS[i].id, res: res });
    await this.recover();
    return;
  }
  this.results[CHECKS[i].id] = res;
  this.log("e", { e: "bt_check", id: CHECKS[i].id, res: res });
};

// After a fall or an ended walk: wait until it's upright again (or the person says ready).
BodyTest.prototype.recover = async function () {
  var e = this.env;
  // A fall or a grab cools the legs off before any walk is admitted again.
  this.setAsk("Catching my balance…");
  await this.wait(T.coolMs);
  if (e.upright() === true) return;
  this.setAsk("I fell. Set me upright, then say “ready”");
  var upSince = null;
  await this.waitFor(["ready", "tap"], function () {
    if (e.upright() === true) { if (upSince === null) upSince = e.now(); return e.now() - upSince >= 2000; }
    upSince = null;
    return false;
  });
};

BodyTest.prototype.checks = async function () {
  var e = this.env;
  this.phase = "run";

  await this.check(0, async function () {
    this.setAsk("Stand still, facing me");
    await this.wait(2500);
    var before = this.recent("fb", 1500);
    this.setAsk("Take one big step to YOUR right");
    await this.wait(4000);
    var after = this.recent("fb", 1500);
    var res = judgeLeftRight(before, after);
    if (res.flip) { try { res.flipped = await e.flip(); } catch (err) { res.flipped = null; } }
    this.setAsk("Step back to the middle, 1 m away");
    await this.wait(3500);
    return res;
  });

  await this.check(1, async function () {
    this.setAsk("Stand still, facing me");
    await this.walk({ heading: 0, speed: 0, pitch: -1 });
    await this.wait(2000);
    var upLow = this.recent("fu", 1500), bLow = this.recent("ob", 1500);
    this.steer({ pitch: 1 });
    await this.wait(3500);
    var upHigh = this.recent("fu", 1500), bHigh = this.recent("ob", 1500);
    this.steer({ pitch: 0 });
    return judgeTilt(upLow, upHigh, bLow, bHigh);
  });

  await this.check(2, async function () {
    this.setAsk("Stay where you are");
    await this.walk({ heading: 0, speed: 0, pitch: 0 });
    await this.wait(T.holdMs);
    var seen = this.mine.filter(function (s) { return typeof s.fb === "number"; }).length;
    var pct = this.mine.length ? Math.round(100 * seen / this.mine.length) : 0;
    var h = this.mine.length ? (this.mine[this.mine.length - 1].h || 0) - (this.mine[0].h || 0) : null;
    return { v: pct >= 70 ? "ok" : "warn", why: "saw you " + pct + "% of the time; turned " + r1(h) + "°", seen: pct, deg: r1(h) };
  });

  await this.check(3, async function () {
    this.setAsk("Stay where you are");
    await this.walk({ heading: 0, speed: 0, pitch: 0 });
    var self = this;
    async function leg(target) {
      var t0 = e.now(), from = self.mine.length;
      self.steer({ heading: target });
      await self.wait(T.turnMs);
      return turnLeg(self.mine.slice(from), target, t0);
    }
    // Each leg is measured from where the last one ended: +90 out, back to 0, -90 out, back.
    var left = await leg(90);
    await leg(0);
    var right = await leg(-90);
    await leg(0);
    return judgeTurn(left, right);
  });

  await this.check(4, async function () {
    this.setAsk("Stay where you are");
    await this.walk({ heading: 0, speed: 0, pitch: 0 });
    await this.wait(1500);
    var d0 = this.recent("fd", 1500), h0 = this.recent("h", 500);
    await this.wait(T.creepMs);
    var d1 = this.recent("fd", 1500), h1 = this.recent("h", 500);
    return judgeCreep(d0, d1, h0, h1, Math.round(T.creepMs / 1000));
  });

  await this.check(5, async function () {
    this.setAsk("Stay where you are — I'll come toward you");
    await this.walk({ heading: 0, speed: 0, pitch: 0 });
    var d0 = this.recent("fd", 1500);
    this.steer({ speed: 1 });
    var self = this;
    await this.wait(T.walkMs, function () { var d = self.lastFace ? self.lastFace.dist : null; return d !== null && d < T.nearStop; });
    this.steer({ speed: 0 });
    await this.wait(2500);
    return judgeWalk(d0, this.recent("fd", 1500), 1);
  });

  await this.check(6, async function () {
    this.setAsk("Stay where you are — I'll back away");
    await this.walk({ heading: 0, speed: 0, pitch: 0 });
    var d0 = this.recent("fd", 1500);
    this.steer({ speed: -1 });
    await this.wait(T.walkMs);
    this.steer({ speed: 0 });
    await this.wait(2500);
    return judgeWalk(d0, this.recent("fd", 1500), -1);
  });

  await this.check(7, async function () {
    this.setAsk("I'll back away, then ask you to say “stop stop”");
    await this.walk({ heading: 0, speed: 0, pitch: 0 });
    this.steer({ speed: -1 });
    await this.wait(1000);
    this.drops = 0; this.heardN = 0; this.estopAt = null;
    this.expectStop = true;
    var asked = e.now();
    this.setAsk("Say “stop stop” now");
    var self = this;
    try {
      await this.wait(T.estopMs, function () { return self.estopAt !== null; });
    } finally { this.expectStop = false; }
    var res = judgeEstop(this.estopAt !== null ? this.estopAt - asked : null, this.heardN, this.drops);
    this.legsOff();
    return res;
  });

  this.legsOff();
  this.results.falls = judgeFalls(this.falls);
};

BodyTest.prototype.run = async function () {
  this.log("e", { e: "bt_start", skills: this.skills, opts: this.opts });
  try {
    await this.setup();
    await this.checks();
    this.status = "done";
    this.phase = "result";
    this.ask = "";
  } catch (err) {
    this.legsOff();
    this.phase = "result";
    if (err instanceof Cancelled) { this.status = "stopped"; this.log("e", { e: "bt_stopped", why: err.why }); }
    else { this.status = "failed"; this.log("e", { e: "bt_error", why: String((err && err.message) || err).slice(0, 200) }); }
  }
  this.log("e", { e: "bt_end", status: this.status, results: this.results });
  this.env.show(this.view());
  await this.flush(true);
  return this.status;
};

// ---- the page's hooks -------------------------------------------------------------------------

BodyTest.prototype.cancel = function (why) { if (!this.dead && this.phase !== "result") this.dead = why || "cancelled"; };

// A tap on the screen's button: what the step waits for.
BodyTest.prototype.tap = function (what) {
  if (what === "cancel") { this.cancel("tap"); return; }
  if (this.waiting && this.waiting.indexOf("tap") >= 0) this.got = "tap";
};

// A transcript: true when the test took it (the creature's model never sees it then).
BodyTest.prototype.heard = function (txt) {
  if (this.phase === "result") return false;
  if (WORDS.silence.test(String(txt || ""))) return true;
  var w = heardWord(txt);
  this.heardN++;
  this.log("e", { e: "bt_heard", word: w, words: String(txt || "").split(/\s+/).filter(Boolean).length, txt: w ? String(txt).slice(0, 60) : undefined });
  if (w === "cancel") { this.cancel("voice"); return true; }
  // "stop stop" with no walk to halt (setup, between checks, after a fall) still ends the test,
  // as the screen promises; gbEStop only logs an estop when the legs were moving.
  if (WORDS.stop2.test(String(txt || "")) && !this.expectStop) { this.cancel("stop stop"); return true; }
  if (this.waiting && w && this.waiting.indexOf(w) >= 0) this.got = w;
  return true;
};

// Every diary row the page logs while the test runs (walk_start/end, skill, gait_abort, estop).
BodyTest.prototype.event = function (e, d) {
  if (this.phase === "result") return;
  var row = { e: e };
  // The row's own kind and time stay its own (follow_calibrate carries a `k`).
  if (d && typeof d === "object") for (var k in d) {
    if (Object.prototype.hasOwnProperty.call(d, k) && typeof d[k] !== "object") row[k === "k" || k === "t" || k === "e" ? "_" + k : k] = d[k];
  }
  this.log("ev", row);
  if (e === "gait_abort") { this.falls++; if (this.walking) { this.walking = false; this.fell = "fell: " + (row.why || "abort"); } }
  if (e === "self_drop" || e === "heard_drop") this.drops++;
  if (e === "estop") {
    if (this.expectStop) { if (this.estopAt === null) this.estopAt = this.env.now(); this.walking = false; }
    else this.cancel("stop stop");
  }
};

// What the screen shows now (growbot-box.js draws it).
BodyTest.prototype.view = function () {
  var tl = this.lastTele || {}, f = this.lastFace, self = this;
  return {
    phase: this.phase,
    setupIdx: this.setupIdx,
    setup: SETUP[this.setupIdx],
    say: this.say,
    waiting: this.waiting,
    secs: Math.round(this.elapsed() / 1000),
    checkIdx: this.checkIdx,
    checks: CHECKS.map(function (c, i) {
      var r = self.results[c.id];
      return { name: c.name, state: r ? r.v : i === self.checkIdx && self.phase === "run" ? "now" : "todo", why: r ? r.why : "" };
    }),
    falls: this.results.falls || null,
    ask: this.ask,
    status: this.status,
    face: f,
    tele: tl,
    bodyUp: this.env.bodyUp(),
    rowsSent: this.seq,
    run: this.run_id,
  };
};
