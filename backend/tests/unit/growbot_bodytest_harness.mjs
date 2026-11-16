// Drives growbot-bodytest.js's whole script against a fake robot, person and clock, for
// test_growbot_bodytest.py. argv: the module's file URL, then a JSON scenario:
//   stepSign   +1 (the frames run as assumed) or -1 (reversed) for the person's step right
//   tiltSign   +1 (+pitch lifts the camera toward the face) or -1 (reversed)
//   fallIn     a check id whose walk tips over once
//   estop      whether "stop stop" stops the walk in the last check (default true)
//   drops      self_drop rows logged in the last check
//   cancelIn   a step id ("place", "hold", ...) in which the screen's Stop is tapped
//   stopStopIn a check id in which "stop stop" halts the legs (the test must end)
//   noFace     the camera never sees anyone during this check id
//   upright    false: the robot never reads upright; "hand": it lies flat until the screen asks
//              to be stood up by hand, then reads upright
//              "lying": it lies flat until env.standUp is called (a get-up that works)
//   standUp    what env.standUp answers ("getup", "tall", or "" when the get-up refuses)
//   bodyLate   ms before the legs answer; a tap before then must not skip the power step
//   sayIn      {step: line}: the person says that line once during that step
//   neverReady true: nobody ever answers the first step
const m = await import(process.argv[2]);
const sc = JSON.parse(process.argv[3] || "{}");
let now = 1e6, heading = 0, cmd = { heading: 0, speed: 0, pitch: 0 }, walking = false, dist = 1.0, bearing = 0;
const posts = [], drives = [], out = { flips: 0, legsOff: 0, standUps: 0, calibrations: 0, walks: 0, shown: 0 };
let test, fell = false, said = {};
const step = () => test.stepName();
const env = {
  now: () => now,
  sleep: async (ms) => {
    now += ms;
    if (walking) {
      heading += Math.max(-3, Math.min(3, (cmd.heading - heading) * 0.1));
      dist -= cmd.speed * 0.002;
      if (sc.fallIn === step() && !fell && cmd.speed !== 0) { fell = true; walking = false; test.event("gait_abort", { why: "tipped" }); }
      if (sc.stopStopIn === step() && !said.stopStop) { said.stopStop = true; walking = false; test.event("estop", { kind: "walk" }); }
    }
    if (sc.cancelIn === step() && !said.cancel) { said.cancel = true; test.tap("cancel"); }
    const ask = test.ask || "";
    if (/YOUR right/.test(ask)) bearing = Math.max(-20, Math.min(20, bearing + (sc.stepSign || 1)));
    else if (/middle/.test(ask)) bearing += bearing > 0 ? -1 : bearing < 0 ? 1 : 0;
    const line = (sc.sayIn || {})[step()];
    if (line && !said["say:" + step()]) { said["say:" + step()] = true; test.heard(line); }
    const w = sc.neverReady && step() === "place" ? null : test.waiting;
    if (w) {
      if (w.includes("ready")) test.heard("I'm ready");
      else if (w.includes("stand")) test.heard("Stand up.");
      else if (w.includes("metre")) test.heard("One meter.");
      else if (w.includes("tap") && !w.includes("ready") && step() === "power" && !said.powerTap) { said.powerTap = true; test.tap("tap"); }
    }
    if (/stop stop/.test(ask) && /now/.test(ask)) {
      if (!said.drops) { said.drops = true; for (let i = 0; i < (sc.drops || 0); i++) test.event("self_drop", { ms: 900 }); }
      if (sc.estop !== false && walking && !said.estop && now % 3000 === 0) { said.estop = true; walking = false; test.event("estop", { kind: "walk" }); }
    }
  },
  show: () => { out.shown++; },
  bodyUp: () => now - 1e6 >= (sc.bodyLate || 0),
  upright: () => {
    if (sc.upright === false) return false;
    if (sc.upright === "lying") return out.standUps > 0;
    if (sc.upright === "hand") { if (/by hand/.test(test.say || "")) said.hand = true; return !!said.hand; }
    return true;
  },
  standUp: () => { out.standUps++; return sc.standUp === undefined ? "tall" : sc.standUp; },
  face: () => sc.noFace === step() ? null : ({ t: now, bearing: bearing - heading, up: 10 - (sc.tiltSign || 1) * cmd.pitch * 4, dist, n: 1, box: { x: 0.4, y: 0.2, width: 0.1, height: 0.1 } }),
  tele: () => ({ h: heading, ob: 80 + cmd.pitch * 5, mode: "look", up: true }),
  newId: () => "bt-test-run1",
  newDrive: () => { const d = "d" + String(drives.length).padStart(7, "0"); drives.push(d); return d; },
  walk: () => { walking = true; out.walks++; return ""; },
  steer: (d, h, s, p) => { if (!walking || d !== drives[drives.length - 1]) return false; cmd = { heading: h, speed: s, pitch: p }; return true; },
  legsOff: () => { walking = false; out.legsOff++; },
  camFront: () => true,
  calibrate: async () => { out.calibrations++; return 1.07; },
  flip: async () => { out.flips++; return true; },
  still: () => (sc.stills ? "data:image/jpeg;base64,AAAA" : null),
  post: async (c) => { posts.push(JSON.parse(JSON.stringify(c))); },
};
test = new m.BodyTest(env, { look: "pol-look", forward: "pol-fwd" });
const status = await test.run();
const rows = posts.flatMap((p) => p.rows);
const kinds = {};
rows.forEach((r) => { kinds[r.k] = (kinds[r.k] || 0) + 1; });
const samples = rows.filter((r) => r.k === "s");
out.heardAfter = test.heard("hello");
console.log(JSON.stringify({
  status, results: test.results, out, secs: (now - 1e6) / 1000,
  seqs: posts.map((p) => p.seq), statuses: posts.map((p) => p.status),
  skills: posts.map((p) => p.skills || null), last: posts[posts.length - 1].summary,
  kinds, sampleHz: samples.length / ((samples[samples.length - 1].t - samples[0].t) / 1000),
  walkAt: rows.filter((r) => r.e === "bt_walk").map((r) => r.t - 1e6),
  abortAt: rows.filter((r) => r.e === "gait_abort").map((r) => r.t - 1e6),
  stopped: rows.filter((r) => r.e === "bt_stopped").map((r) => r.why),
  heardN: rows.filter((r) => r.e === "bt_heard").length,
  events: rows.filter((r) => r.k === "e" || r.k === "ev").map((r) => r.e),
  steps: rows.filter((r) => r.e === "bt_step").map((r) => r.step),
  stills: rows.filter((r) => r.k === "still").map((r) => r.step),
  heard: rows.filter((r) => r.e === "bt_heard").slice(0, 3),
  stands: rows.filter((r) => r.e === "bt_stand").map((r) => [r.ok, r.how]),
  faces: rows.filter((r) => r.e === "bt_face").map((r) => ({ seen: r.seen, fb: r.fb, after: r.after })),
  faceRow: samples.find((r) => typeof r.fb === "number") || null,
  stepAt: Object.fromEntries(rows.filter((r) => r.e === "bt_step").map((r) => [r.step, r.t - 1e6])),
}));
