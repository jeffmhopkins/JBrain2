// GrowBot follow: a face in the front camera becomes growbot-drive.js's command.
//
// WHY THIS EXISTS (docs/plans/GROWBOT_POLICY_DRIVE_PLAN.md, wave P3): the face boxes the creature
// already detects (growbot-eyes.js, from upstream's BlazeFace) are turned into a bearing and a
// distance, one person is kept through crossings, and a follower turns that into heading, speed
// and camera height. Pure functions and small classes only, so Node tests drive them; the page
// glue is in growbot-box.js (gbFollow).
//
// THE FRAME (owner, 2026-11-15): the front camera points out the camera end. Its frames are not
// mirrored, and the robot's left is the phone's -x (growbot-box.js bodyAccel), which is the
// screen's left, which is the frame's RIGHT. So a face right of the frame's centre is to the
// robot's left: a positive (counter-clockwise, turn left) bearing. `flipX` undoes that if a
// phone's frames turn out the other way (the dry run shows it at a glance).
//
// No `??` or `?.`: the creature's page runs on old iPhones (growbot_runner.js).

var D = Math.PI / 180;

export var CAMERA = {
  hfov: 60,          // degrees across the frame's width; calibrate per phone
  vfov: 45,
  faceW: 0.15,       // metres: a face box's width, so its width in the frame gives distance
  k: 1,              // the person's own correction, from one "stand here at 1 m"
  flipX: false,
};

export var FOLLOW = {
  near: 0.4, far: 3,       // the set distance's limits (owner, 2026-11-15)
  step: 0.25,              // "get closer" / "back up"
  band: 0.15,              // metres either side of the set distance where it stands and turns
  gain: 1.5,               // speed per metre of distance error, before the clamp
  backMax: 1,              // the furthest back a speed goes
  staleMs: 500,            // no reading this long: speed 0
  lostMs: 3000,            // no reading this long: lost
  coastMs: 2000,           // a track keeps its prediction this long through a crossing
  pitchGain: 0.02,         // camera height per degree of vertical offset, per reading
};

function num(v, d) { return typeof v === "number" && isFinite(v) ? v : d; }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function wrap180(d) { return ((d + 180) % 360 + 360) % 360 - 180; }

// One face box ({x, y, width, height}, 0..1 of the frame) as the robot sees it: the bearing
// (deg, + left), the vertical offset (deg, + above centre) and the distance (m).
export function reading(box, cam) {
  cam = cam || CAMERA;
  var cx = box.x + box.width / 2, cy = box.y + box.height / 2;
  var tx = Math.tan(num(cam.hfov, 60) / 2 * D), ty = Math.tan(num(cam.vfov, 45) / 2 * D);
  var side = (cx - 0.5) * 2 * (cam.flipX ? -1 : 1);
  var bearing = Math.atan(side * tx) / D;
  var up = Math.atan((0.5 - cy) * 2 * ty) / D;
  // The face's angular width against its real width; small-angle is plenty at a metre or more.
  var ang = 2 * Math.atan(Math.max(1e-4, box.width) * tx);
  var dist = num(cam.k, 1) * num(cam.faceW, 0.15) / (2 * Math.tan(ang / 2));
  return { bearing: bearing, up: up, dist: dist, size: box.width };
}

// The calibration's correction: the person stood at `metres` and the box read `raw` metres.
export function calibrate(raw, metres) {
  return raw > 0 && metres > 0 ? metres / raw : 1;
}

// One person, kept by where they should be. Bearings are stored in the walk's own frame
// (the robot's heading at the reading plus its bearing), so the robot turning doesn't look
// like the person moving; `heading` is the robot's integrated heading in degrees.
export function Track(opts) {
  this.o = opts || FOLLOW;
  this.p = null;     // {az, dist, size, vaz, vdist, t, up}: the person, az in the walk's frame
  this.seq = 0;      // bumped on every reading taken, so the follower integrates each once
}
Track.prototype.predict = function (t) {
  var p = this.p;
  if (!p) return null;
  var dt = Math.min(this.o.coastMs, Math.max(0, t - p.t)) / 1000;
  return { az: p.az + p.vaz * dt, dist: Math.max(0.1, p.dist + p.vdist * dt), size: p.size };
};
// Lock onto one of these readings: the nearest, then the most central (P4 adds the speaker).
Track.prototype.lock = function (reads, heading, t) {
  var ok = (reads || []).filter(function (r) { return r && isFinite(r.bearing) && isFinite(r.dist); });
  if (!ok.length) return false;
  ok.sort(function (a, b) { return (a.dist - b.dist) || (Math.abs(a.bearing) - Math.abs(b.bearing)); });
  var r = ok[0];
  this.p = { az: wrap180(heading + r.bearing), dist: r.dist, size: r.size, vaz: 0, vdist: 0, t: t, up: r.up };
  this.seq++;
  return true;
};
// Take this frame's readings: keep the one that matches the prediction, if any does. Returns
// whether the person was seen. A reading far from the prediction is never taken just because
// it is the only one; one close to it is, and of two equally close (people meeting at the same
// spot), the nearer the prediction wins: the best guess (owner, 2026-11-15).
Track.prototype.update = function (reads, heading, t) {
  var q = this.predict(t);
  if (!q) return false;
  // How far round they could have moved since: a brisk walk past it, plus the readings' noise.
  var gate = 10 + 60 * Math.min(this.o.coastMs, Math.max(0, t - this.p.t)) / 1000;
  var best = null, bestCost = Infinity;
  (reads || []).forEach(function (r) {
    if (!r || !isFinite(r.bearing) || !isFinite(r.dist)) return;
    var az = wrap180(heading + r.bearing);
    var dAz = Math.abs(wrap180(az - q.az)), dD = Math.abs(r.dist - q.dist) / Math.max(0.3, q.dist);
    var dS = q.size > 0 ? Math.abs(Math.log(Math.max(1e-3, r.size) / q.size)) : 0;
    if (dAz > gate || dD > 0.5 || dS > 0.7) return;
    var cost = dAz / gate + dD / 0.5 + dS / 0.7;
    if (cost < bestCost) { bestCost = cost; best = { az: az, r: r }; }
  });
  if (!best) return false;
  var p = this.p, dt = Math.max(0.05, (t - p.t) / 1000), a = 0.5;
  var vaz = wrap180(best.az - p.az) / dt, vdist = (best.r.dist - p.dist) / dt;
  // A light smoothing: readings are noisy, and the policy trained on 3 degrees of noise.
  this.p = {
    az: wrap180(p.az + a * wrap180(best.az - p.az)),
    dist: p.dist + a * (best.r.dist - p.dist),
    size: best.r.size,
    vaz: clamp(0.5 * p.vaz + 0.5 * vaz, -90, 90),
    vdist: clamp(0.5 * p.vdist + 0.5 * vdist, -1.5, 1.5),
    t: t,
    up: best.r.up,
  };
  this.seq++;
  return true;
};
Track.prototype.age = function (t) { return this.p ? t - this.p.t : Infinity; };

// The follower: the person, as tracked, into growbot-drive.js's command. `set` is the distance
// to hold, taken from the person when following starts (owner, 2026-11-15). `stay` holds speed
// at 0 while it keeps turning and tilting to watch.
export function Follower(opts) {
  this.o = opts || FOLLOW;
  this.set = 1;
  this.pitch = 1;
  this.stay = false;
  this.seq = -1;
}
Follower.prototype.start = function (dist) {
  this.set = clamp(num(dist, 1), this.o.near, this.o.far);
};
Follower.prototype.closer = function () { this.set = clamp(this.set - this.o.step, this.o.near, this.o.far); return this.set; };
Follower.prototype.further = function () { this.set = clamp(this.set + this.o.step, this.o.near, this.o.far); return this.set; };
// The command for now: the track (Track), the robot's heading (deg) and the time (ms).
// `state` says why: "follow", "hold" (in the band, on stay, or a stale reading), or "lost"
// (it turns toward where the person was last seen and stands: it never re-locks onto someone
// else by itself, the Track's match does that only for the same person).
Follower.prototype.command = function (track, heading, t) {
  var age = track.age(t), p = track.p;
  if (!p) return { heading: 0, speed: 0, pitch: this.pitch, state: "lost" };
  if (age > this.o.lostMs) return { heading: wrap180(p.az - heading), speed: 0, pitch: this.pitch, state: "lost" };
  // Coast on the prediction only while readings are fresh: a stale one holds where they were
  // heading at that moment, so it never spins off on old speed and snaps back when lost.
  var q = track.predict(Math.min(t, p.t + this.o.staleMs)), err = wrap180(q.az - heading);
  var speed = 0, state = "hold";
  if (age <= this.o.staleMs && !this.stay) {
    var off = q.dist - this.set;
    if (Math.abs(off) > this.o.band) {
      speed = clamp(this.o.gain * (off - (off > 0 ? this.o.band : -this.o.band)), -this.o.backMax, 1);
      state = "follow";
    }
  }
  // The camera's height creeps toward the face once per reading, not once per tick.
  if (track.seq !== this.seq) {
    this.seq = track.seq;
    if (isFinite(p.up)) this.pitch = clamp(this.pitch + this.o.pitchGain * p.up, -1, 1);
  }
  return { heading: err, speed: speed, pitch: this.pitch, state: state, dist: q.dist, set: this.set };
};
