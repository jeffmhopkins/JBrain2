// tests/unit/test_growbot_site.py runs growbot-box.js's trained walk here, in node, against
// stubs of the next.html globals it drives. Arguments: the runner's file URL, growbot-box.js,
// the pad's move, the phone's IMU as JSON, optionally the legs' duty left (ms), and optionally
// the Body tab's steering as JSON ({at, drive, heading, speed, pitch}: sent `at` ms after a move
// whose Drive id is "d1ce0001"). Env: GB_SKILLS runs a skill set (growbot-drive.js at GB_DRIVE)
// of three stand-in files instead, and GB_ESTOP_AT says "stop, stop!" that many ms in.
// GB_FOLLOW ("walk" or "dry") follows faces instead (growbot-follow.js at GB_FOLLOW_JS): GB_FACES
// is [{at, x, w}], a square face box centred at x (0..1 of the frame) and w wide, seen `at` ms
// in; GB_WAIT is how long to watch.
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
g._bodyAlive = () => true; g.ensureIMU = () => {}; g.holdStop = () => {}; g.logEv = () => {};
g.phoneTiltDegrees = () => 0; g.setErr = (m) => errs.push(m);
g.calNow = () => ({ L_OFF: 4, R_OFF: -2, L_SIGN: -1, R_SIGN: 1, IMU_SIGN: [1, -1, 1], gain: 0.7 });
g.$ = () => ({ textContent: "" });
// The eyes' feed, the creature's screen and its camera, for the follower.
let eyes = null;
const shown = [];
g.gbEyes = { listen(fn) { eyes = fn; } };
g.camOn = true; g.camFacing = "user";
g.document.body = { appendChild(el) { shown.push(el); } };
g.document.createElement = () => ({ style: {}, innerHTML: "", remove() {} });
// The legs' duty budget, as next.html keeps it: a run is granted no more than what is left.
g._walkLog = [];
g.dutyLeftMs = () => Math.max(0, Number(process.argv[6] || 60000) - g._walkLog.reduce((t, w) => t + w.ms, 0));
g.policySeconds = (v, fallback, ms) => Math.min(Number(v) > 0 ? Math.max(1, Number(v)) : fallback, ms / 1000);
let seq = 0;
g._moveRequest = (name) => ({ id: ++seq, action: name, state: "requested" });
g._moveReport = (a, state, why) => { a.state = state; reports.push([state, why]); };
g.stopMotion = () => { g._motionGen++; if (g._motion && g._motion.loopIv) clearTimeout(g._motion.loopIv); g._motion = null; };
g.carryLegMotion = (rest) => { g._motionGen++; if (g._motion && g._motion.loopIv) clearTimeout(g._motion.loopIv); g._motion = null; if (rest) poses.push(["rest"]); return true; };
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
g.fetch = async (url) => {
  const id = decodeURIComponent(url.split("id=")[1] || "");
  return { ok: url.startsWith("/api/box/policy?id="), status: 200, json: async () => FILES[id] || POLICY };
};
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
if (FOLLOW === "dry") g.gbFollow.dry(SKILLS);
else g.gbTrainedWalk(SKILLS || "0f3e9a6c-1b2d-4c5e-8f90-a1b2c3d4e5f6", MOVE, 1, STEER ? "d1ce0001" : "", FOLLOW === "walk");
let estopped = null, estopAt = -1;
if (process.env.GB_ESTOP_AT) {
  setTimeout(() => { estopped = [g.gbEStop("stop"), g.gbEStop("stop, stop!")]; estopAt = fed.length; }, Number(process.env.GB_ESTOP_AT));
}
const accepted = reports[0];
let steered = null;
if (STEER) setTimeout(() => { steered = g.gbSteer(STEER); }, STEER.at);
await new Promise((r) => setTimeout(r, Number(process.env.GB_WAIT || 0) || (STEER ? 3600 : 2400)));
const screen = shown.length ? shown[shown.length - 1].innerHTML : "";
console.log(JSON.stringify({ screen, logs: logs.map((l) => l[0]), accepted, steered, estopped, estopAt, files: fed.map((f) => f.file), cmds: fed.map((f) => f.cmd), reports, walkLog: g._walkLog, fed: fed.slice(0, 3), tail: fed.slice(-2), n: fed.length, poses: poses.slice(0, 2), last: poses[poses.length - 1], errs }));
if (FOLLOW) process.exit(0);
