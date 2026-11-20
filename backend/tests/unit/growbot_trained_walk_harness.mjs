// tests/unit/test_growbot_site.py runs growbot-box.js's trained walk here, in node, against
// stubs of the next.html globals it drives. Arguments: the runner's file URL, growbot-box.js,
// the pad's move, the phone's IMU as JSON, optionally the legs' duty left (ms), and optionally
// the Body tab's steering as JSON ({at, drive, heading, speed, pitch}: sent `at` ms after a move
// whose Drive id is "d1ce0001"). Env: GB_SKILLS runs a skill set (growbot-drive.js at GB_DRIVE)
// of three stand-in files instead, and GB_ESTOP_AT says "stop, stop!" that many ms in.
// GB_FOLLOW ("walk" or "dry") follows faces instead (growbot-follow.js at GB_FOLLOW_JS): GB_FACES
// is [{at, x, w}], a square face box centred at x (0..1 of the frame) and w wide, seen `at` ms
// in; GB_WAIT is how long to watch. GB_TURN is a "turn" move's degrees and GB_SECS its time.
// GB_GO runs a route (gbOwnGo) on the skill set instead: [{n, heading, speed} | {done}], each
// answered for n ticks in turn, with a forward ask of his own 1.5 s in.
import { register } from "node:module";
const RUNNER = process.argv[2], BOX = process.argv[3], MOVE = process.argv[4];
const PHONE = JSON.parse(process.argv[5]);
register("data:text/javascript," + encodeURIComponent(
  "export async function resolve(s, c, n) {" +
  " if (s === '/growbot-drive.js') return n(" + JSON.stringify(process.env.GB_DRIVE || "") + ", c);" +
  " if (s === '/growbot-follow.js') return n(" + JSON.stringify(process.env.GB_FOLLOW_JS || "") + ", c);" +
  " return n(s === '/growbot_runner.js' ? " + JSON.stringify(RUNNER) + " : s, c); }"));
const fs = await import("node:fs");
const runner = await import(RUNNER);
const fed = [], poses = [], reports = [], errs = [];
const real = runner.Walker.prototype.step;
runner.Walker.prototype.step = function (A, G, cmd) {
  fed.push({ A, G, cmd, file: this.spec.segments[0].channels.join(",") });
  return real.call(this, A, G, cmd);
};
const g = globalThis;
g.window = g;
g.location = { pathname: "/v3/next.html", search: "" };
g._wheels = false; g.paused = false; g.dreaming = false; g.document = { hidden: false };
g.ori = { a: 0, b: 0, g: 0 }; g.accel = PHONE.accel; g.rate = PHONE.rate; g.linMag = 0;
g._motion = null; g._motionGen = 0; g.selfMoveUntil = 0; g._gaitCoolUntil = 0; g._imuWhy = "";
g._body = { ready: true, pose(l, r) { poses.push([l, r]); } };
// GB_POLICY runs that one file from FILES alone instead of the default heading-only POLICY.
// GB_SIMMATCH runs the Body tab's Sim match (gbSimMatch) instead of a walk.
// GB_HELD: the legs held at a pose when the walk starts (a rest's hold), as _legStart reads it.
if (process.env.GB_HELD) { const held = JSON.parse(process.env.GB_HELD); g._legStart = () => ({ mode: "anchor", anchor: held }); }
g._bodyAlive = () => true; g.ensureIMU = () => {}; g.holdStop = () => {}; g.logEv = () => {};
// GB_TIP_AT: the phone reads 60 degrees off its stance from that many ms in (a tip).
const tipAt = Number(process.env.GB_TIP_AT || 0), bornT = Date.now();
g.phoneTiltDegrees = () => (tipAt && Date.now() - bornT >= tipAt ? 60 : 0); g.setErr = (m) => errs.push(m);
g.calNow = () => ({ L_OFF: 4, R_OFF: -2, L_SIGN: -1, R_SIGN: 1, IMU_SIGN: [1, -1, 1], gain: 0.7 });
g.$ = () => ({ textContent: "" });
// The eyes' feed, the creature's screen and its camera, for the follower.
let eyes = null;
const shown = [];
g.gbEyes = { listen(fn) { eyes = fn; } };
g.camOn = true; g.camFacing = process.env.GB_CAM || "user";
g.document.body = { appendChild(el) { shown.push(el); } };
g.document.createElement = () => ({ style: {}, innerHTML: "", remove() {} });
// The legs' duty budget, as next.html keeps it: a run is granted no more than what is left.
g._walkLog = [];
g.dutyLeftMs = () => Math.max(0, Number(process.argv[6] || 60000) - g._walkLog.reduce((t, w) => t + w.ms, 0));
g.policySeconds = (v, fallback, ms) => Math.min(Number(v) > 0 ? Math.max(1, Number(v)) : fallback, ms / 1000);
let seq = 0;
g._moveLast = null;
g._moveRequest = (name) => ({ id: ++seq, action: name, state: "requested" });
g._moveReport = (a, state, why) => { a.state = state; reports.push([state, why]); };
g.stopMotion = () => { g._motionGen++; if (g._motion && g._motion.loopIv) clearTimeout(g._motion.loopIv); g._motion = null; };
g.carryLegMotion = (rest) => { g._motionGen++; if (g._motion && g._motion.loopIv) clearTimeout(g._motion.loopIv); g._motion = null; if (rest && g._imuWhy === "done") poses.push(["rest"]); return true; };
const POLICY = {
  obs_size: 2, act_size: 2, mean: [0, 0], std: [1, 1],
  layers: [{ W: [[0, 0], [1, -1]], b: [0, 0], act: "linear" }],
  obs_spec: { control_hz: 25, imu_frame: { up: "+z" },
    segments: [{ channels: ["gyro_z", "cmd_yaw"], frames: 1 }],
    cmd_yaw: { meaning: "heading_error", facing: "nose" } },
};
// The skill set's stand-ins, told apart by their channels: a look, the tall walk, the nose walk.
function file(chans, facing, training, fwd) {
  const n = chans.length;
  return { obs_size: n, act_size: 2, mean: Array(n).fill(0), std: Array(n).fill(1),
    layers: [{ W: Array.from({ length: n }, () => [0, 0]), b: [0, 0], act: "linear" }],
    obs_spec: { control_hz: 25, imu_frame: { up: "+z" }, segments: [{ channels: chans, frames: 1 }],
      cmd_yaw: { meaning: "heading_error", facing }, ...(fwd ? { cmd_forward: { range: fwd } } : {}) },
    training };
}
const FILES = {
  "11111111-1111-4111-8111-111111111111": file(["gyro_z", "cmd_yaw", "cmd_pitch"], "camera", { speed: "still", speed_cap_m_s: 0.03 }),
  "22222222-2222-4222-8222-222222222222": file(["gyro_z", "cmd_yaw"], "camera", { speed: "cap", speed_cap_m_s: 0.05 }),
  "33333333-3333-4333-8333-333333333333": file(["gyro_z", "cmd_yaw", "cmd_forward"], "nose", { speed: "command", speed_cap_m_s: 0.03 }, [0, 1]),
};
const GO = process.env.GB_GO ? JSON.parse(process.env.GB_GO) : null;
// The robot this phone wears (btRobot), so the box's skill set is read for gbOwnGo.
if (GO) { g.location.search = "?id=gb-r1"; g.document.addEventListener = () => {}; }
// GB_REC watches the flight recorder: the robot id it posts under, its posts, and the page's
// pagehide, fired once the walk is watched.
const recPosts = [], pageOn = {};
if (process.env.GB_REC) { g.location.search = "?id=gb-r1"; g.addEventListener = (k, f) => { pageOn[k] = f; }; }
g.fetch = async (url, opts) => {
  if (url === "/api/box/bodytest") { recPosts.push(JSON.parse(opts.body)); return { ok: true, status: 200, json: async () => ({}) }; }
  if (url.startsWith("/api/box/skills")) return { ok: true, status: 200, json: async () => ({ skills: JSON.parse(process.env.GB_SKILLS) }) };
  const id = decodeURIComponent(url.split("id=")[1] || "");
  return { ok: url.startsWith("/api/box/policy?id="), status: 200, json: async () => FILES[id] || POLICY };
};
// GB_MIRROR: Settings' Mirror face tracking on, the page's own block (built next.html) beside BOX.
if (process.env.GB_MIRROR) {
  const page = fs.readFileSync(BOX.replace(/growbot-box\.js$/, "v3/next.html"), "utf8");
  (0, eval)(page.slice(page.indexOf("/*FACE-MIRROR{*/"), page.indexOf("/*}FACE-MIRROR*/")) + "\nsetFaceMirror(true);");
}
const src = fs.readFileSync(BOX, "utf8");
(0, eval)(src.slice(src.indexOf("/* Trained walks from JBrain's policy library")));
const STEER = process.argv[7] ? JSON.parse(process.argv[7]) : null;
const SKILLS = process.env.GB_SKILLS ? JSON.parse(process.env.GB_SKILLS) : null;
const FOLLOW = process.env.GB_FOLLOW || "";
const FACES = process.env.GB_FACES ? JSON.parse(process.env.GB_FACES) : [];
const logs = [];
g.logEv = (k, v) => logs.push([k, v]);
FACES.forEach((f) => setTimeout(() => {
  const h = f.w * 0.75;
  if (eyes) eyes(f.w ? [{ x: f.x - f.w / 2, y: 0.4 - h / 2, width: f.w, height: h }] : []);
}, f.at));
let go = null, goCalls = 0, goAsk = null;
if (GO) {
  await new Promise((r) => setTimeout(r, 0));
  let k = 0, used = 0;
  go = g.gbOwnGo(() => {
    goCalls++;
    while (GO[k].n !== undefined && used >= GO[k].n) { k++; used = 0; }
    used++;
    return GO[k];
  }, Number(process.env.GB_SECS || 1));
  // The model's own forward ask in the middle of it (the walk look's "send walk again").
  setTimeout(() => { const a = g._moveRequest("walk"); goAsk = [g.gbOwnWalk({}, "fwd", a), a.state, a.reason]; }, 1500);
} else if (process.env.GB_SIMMATCH) g.gbSimMatch();
else if (FOLLOW === "dry") g.gbFollow.dry(SKILLS);
else g.gbTrainedWalk(SKILLS || process.env.GB_POLICY || "0f3e9a6c-1b2d-4c5e-8f90-a1b2c3d4e5f6", MOVE, Number(process.env.GB_SECS || 1), STEER ? "d1ce0001" : "", FOLLOW === "walk", Number(process.env.GB_TURN || 0));
let estopped = null, estopAt = -1;
if (process.env.GB_ESTOP_AT) {
  setTimeout(() => { estopped = [g.gbEStop("stop"), g.gbEStop("stop, stop!")]; estopAt = fed.length; }, Number(process.env.GB_ESTOP_AT));
}
const accepted = reports[0];
let steered = null;
if (STEER) setTimeout(() => { steered = g.gbSteer(STEER); }, STEER.at);
// What the body test samples off the walk (growbot-box.js M.tele), as last seen.
let tele = null;
const teleIv = setInterval(() => { if (g._motion && g._motion.tele) tele = g._motion.tele; }, 20);
if (process.env.GB_REC) setTimeout(() => g.logEv("act", { said: "my name is Quorra", why: "fwd", n: 3, ok: true, o: { a: 1 } }), 500);
await new Promise((r) => setTimeout(r, Number(process.env.GB_WAIT || 0) || (STEER ? 3600 : 2400)));
clearInterval(teleIv);
let rec;
if (process.env.GB_REC) {
  pageOn.pagehide();
  await new Promise((r) => setTimeout(r, 120));
  // Back from the bfcache: the next leg event records into a fresh run.
  g.logEv("walk_start", {});
  await new Promise((r) => setTimeout(r, 120));
  pageOn.pagehide();
  await new Promise((r) => setTimeout(r, 50));
  rec = recPosts.map((c) => ({ run: c.run, seq: c.seq, status: c.status, robot: c.robot, kind: c.summary.kind, moves: c.summary.moves,
    n: c.rows.length, samples: c.rows.filter((r) => r.k === "s").length, lc: c.rows.filter((r) => r.k === "s" && r.lc).length,
    events: c.rows.filter((r) => r.k === "e") }));
}
const screen = shown.length ? shown[shown.length - 1].innerHTML : "";
console.log(JSON.stringify({ go: go && { state: go.state, mv: go.mv, why: go._gbGo }, goCalls, goAsk, tele, screen, logs: logs.map((l) => l[0]), ends: logs.filter((l) => l[0] === "walk_end").map((l) => l[1]), accepted, steered, estopped, estopAt, files: fed.map((f) => f.file), cmds: fed.map((f) => f.cmd), reports, walkLog: g._walkLog, fed: fed.slice(0, 3), tail: fed.slice(-2), n: fed.length, poses: poses.slice(0, 2), allPoses: process.env.GB_HELD || process.env.GB_SIMMATCH ? poses : undefined, lastTele: g._motion && g._motion.tele, at: process.env.GB_SIMMATCH ? [0, 1.5, 2, 9, 14.5, 28.5, 1e3].map((t) => g.gbSimMatchAt(t)) : undefined, rec, last: poses[poses.length - 1], errs }));
if (FOLLOW || process.env.GB_SIMMATCH) process.exit(0);
