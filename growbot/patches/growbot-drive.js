// GrowBot policy drive: one command for every trained skill, and the rule that picks a skill.
//
// WHY THIS EXISTS (requirements doc "GrowBot policy drive", 2026-11-15): until GrowBot_Training's
// master policy exists, look, the tall camera-first walk and the nose-first backing walk are three
// files with different inputs. Everything above them (the Body tab's Drive, and later the face
// follower and the timed walk) speaks ONE command, and this module turns it into each file's own
// inputs, read from that file's obs_spec, so a new checkpoint drops in with no app change.
//
// THE FRAME (owner, 2026-11-15): everything is measured from the phone's FRONT camera, which
// points out the camera end of the body. The command is
//   heading  degrees to turn, positive = counter-clockwise seen from above (turn left), the error
//            between where the camera points and where it should point (the person);
//   speed    -1..+1: above 0 walk toward what the camera sees, below 0 back away from it while the
//            camera keeps facing it (never turning around), 0 stand and turn;
//   pitch    -1 low .. +1 tall: camera height.
// A nose-first file walks away from the camera's view, so its "ahead" is the camera's opposite.
// Its target is "away from the person", which sits half a turn from the person; measured from
// the nose (also half a turn from the camera), the error is the SAME number as the camera's
// error: a turn is a turn whichever end you call ahead. So heading passes through unchanged for
// every file, and only the speed's sign flips (the nose walk's +1 is the camera's backing).
//
// No `??` or `?.` in this file: the creature's page runs on old iPhones (growbot_runner.js).

export var MODES = ["look", "forward", "back"];

// The selector's thresholds. A forward walk starts only when the target is nearly ahead
// (sharp turns are stop, spin with look, then walk: settled 2026-11-15); backing has no
// heading gate, and a mode holds a moment so a speed
// hovering at a threshold doesn't swap files every tick.
export var SPEED_ON = 0.15, SPEED_OFF = 0.05, WALK_TURN_MAX = 30, WALK_TURN_LEAVE = 45, HOLD_MS = 1500;

function num(v, dflt) { return typeof v === "number" && isFinite(v) ? v : dflt; }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function channelList(pj) {
  var sp = (pj && pj.obs_spec) || {}, out = [];
  (Array.isArray(sp.segments) ? sp.segments : [sp]).forEach(function (seg) {
    if (seg && Array.isArray(seg.channels)) out = out.concat(seg.channels);
  });
  return out;
}

// What one policy file takes and how fast it walks, from its own declarations.
export function profile(pj) {
  var sp = (pj && pj.obs_spec) || {}, yaw = sp.cmd_yaw || {}, ch = channelList(pj);
  var fwd = sp.cmd_forward || {}, range = Array.isArray(fwd.range) ? fwd.range : [-1, 1];
  var training = (pj && pj.training) || {};
  if (yaw.meaning !== "heading_error") throw new Error("that policy isn't steered by a heading");
  return {
    facing: yaw.facing === "nose" ? "nose" : "camera",
    hasSpeed: ch.indexOf("cmd_forward") >= 0,
    speedMin: num(range[0], -1),
    hasPitch: ch.indexOf("cmd_pitch") >= 0,
    // speed "still" is a look: it turns on the spot and never walks.
    still: training.speed === "still",
    capMs: num(training.speed_cap_m_s, 0),
    hz: num(sp.control_hz, 25),
  };
}

function wrap180(d) { return ((d + 180) % 360 + 360) % 360 - 180; }

// The file's inputs for one tick of `mode`: {fwd, yaw, pitch} as growbot_runner.js's Walker.step
// takes them (yaw is the heading error over 180), plus the commanded ground speed in m/s.
export function inputs(prof, mode, cmd) {
  var heading = wrap180(num(cmd && cmd.heading, 0)), speed = clamp(num(cmd && cmd.speed, 0), -1, 1);
  var fwd = 0;
  if (mode === "forward") fwd = prof.hasSpeed ? clamp(Math.abs(speed), Math.max(0, prof.speedMin), 1) : 1;
  else if (mode === "back") {
    // Nose first, the walk's own +1 IS backing away; a camera-first file backs on its negatives.
    if (prof.facing === "nose") fwd = prof.hasSpeed ? clamp(Math.abs(speed), Math.max(0, prof.speedMin), 1) : 1;
    else fwd = prof.hasSpeed ? -clamp(Math.abs(speed), 0, -Math.min(0, prof.speedMin)) : 0;
  }
  var out = { fwd: fwd, yaw: heading / 180, ms: mode === "look" || prof.still ? 0 : Math.abs(fwd) * prof.capMs };
  if (prof.hasPitch) out.pitch = clamp(num(cmd && cmd.pitch, 1), -1, 1);
  return out;
}

// The mode a command asks for, before hysteresis. Only the speed's sign picks the walk (owner,
// 2026-11-15): a negative speed is the nose walk whatever the heading, steering as it backs with
// the camera on the person; a target behind never means backing, only a spin (look) first.
export function wanted(cmd, current) {
  var h = Math.abs(wrap180(num(cmd && cmd.heading, 0))), s = num(cmd && cmd.speed, 0);
  var walking = current === "forward" || current === "back";
  var on = walking ? SPEED_OFF : SPEED_ON, turnMax = walking ? WALK_TURN_LEAVE : WALK_TURN_MAX;
  if (s < -on) return "back";
  if (h > turnMax) return "look";
  if (s > on) return "forward";
  return "look";
}

// Picks the skill each tick. `has` says which modes the skill set covers; a missing mode falls
// back to look (and a missing look to standing still, which the caller does).
// Stopping is never delayed; a walk starts only once the current mode has held HOLD_MS, and a
// walk never turns straight into the other walk: it stops (look) first.
export function Selector(has, now) {
  this.has = has || {};
  this.mode = "look";
  this.since = num(now, 0) - HOLD_MS;
}
Selector.prototype.pick = function (cmd, now) {
  now = num(now, 0);
  var want = wanted(cmd, this.mode);
  if (!this.has[want]) want = "look";
  if (want !== "look" && this.mode !== "look") want = want === this.mode ? want : "look";
  if (want !== this.mode && (want === "look" || now - this.since >= HOLD_MS)) {
    this.mode = want;
    this.since = now;
    return { mode: want, changed: true };
  }
  return { mode: this.mode, changed: false };
};
