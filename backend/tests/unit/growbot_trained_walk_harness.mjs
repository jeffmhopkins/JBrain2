// tests/unit/test_growbot_site.py runs growbot-box.js's trained walk here, in node, against
// stubs of the next.html globals it drives. Arguments: the runner's file URL, growbot-box.js,
// the pad's move, and the phone's IMU as JSON.
import { register } from "node:module";
const RUNNER = process.argv[2], BOX = process.argv[3], MOVE = process.argv[4];
const PHONE = JSON.parse(process.argv[5]);
register("data:text/javascript," + encodeURIComponent(
  "export async function resolve(s, c, n) {" +
  " return n(s === '/growbot_runner.js' ? " + JSON.stringify(RUNNER) + " : s, c); }"));
const fs = await import("node:fs");
const runner = await import(RUNNER);
const fed = [], poses = [], reports = [], errs = [];
const real = runner.Walker.prototype.step;
runner.Walker.prototype.step = function (A, G, cmd) { fed.push({ A, G, cmd }); return real.call(this, A, G, cmd); };
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
g.fetch = async (url) => ({ ok: url.startsWith("/api/box/policy?id="), status: 200, json: async () => POLICY });
const src = fs.readFileSync(BOX, "utf8");
(0, eval)(src.slice(src.indexOf("/* Trained walks from JBrain's policy library")));
g.gbTrainedWalk("0f3e9a6c-1b2d-4c5e-8f90-a1b2c3d4e5f6", MOVE, 1);
const accepted = reports[0];
await new Promise((r) => setTimeout(r, 2400));
console.log(JSON.stringify({ accepted, reports, fed: fed.slice(0, 3), n: fed.length, poses: poses.slice(0, 2), last: poses[poses.length - 1], errs }));
