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
  band: 0.15,              // metres too close past which it backs away
  gain: 1.5,               // backing speed per metre past the band, before the clamp
  // Too far is a step forward, as the master's own follower trained it (GrowBot_Training
  // follow.py): a walk starts past walkStart and keeps on until within walkStop, at full speed,
  // so the half turn's own drift (10-25 cm) is taken back in one walk, not left in the band.
  walkStart: 0.10,
  walkStop: 0.05,
  walkTurnMax: 30,         // degrees off the camera a walk starts within (+10 to keep walking)
  // The face's distance swings about 0.3 m with each waddle's pitch, so the follower acts on
  // its average over about this long, and a walk waits until it holds walkWaitMs of readings:
  // acting on each reading chattered between walking and standing (owner, 2026-11-15: "zero
  // creep").
  avgMs: 1500,
  walkWaitMs: 1000,
  backMax: 1,              // the furthest back a speed goes
  staleMs: 500,            // no reading this long: speed 0
  lostMs: 3000,            // no reading this long: lost
  coastMs: 2000,           // a track keeps its prediction this long through a crossing
  pitchGain: 0.02,         // camera height per degree of vertical offset, per reading
  backTurnMax: 60,         // degrees off the camera past which it turns before it backs
  // Only look takes a camera height, and it barely turns with the camera low (GrowBot_Training
  // master follow-me: 11-45 degrees of a 90-degree turn in 10 s at -1, 80-88 at 0). So the
  // height moves only while standing within pitchTurnMax of the person, and a wider turn or a
  // search is sent no lower than pitchTurnMin; the height asked for comes back after.
  pitchTurnMax: 20,
  pitchFaceDeg: 15,        // and comes back only within this, so noise at the limit doesn't flip it
  pitchTurnMin: 0,
  // A face last seen near the frame's edge most likely left over it, and the camera loses it
  // before the robot faces that way, so turning only to where it was last seen stops short of
  // seeing it again: a turn past it, on that side, does (owner, 2026-11-16: "keep on spinning in
  // that direction for another 5 or 10 or 15 degrees"). edgeSide is how far out from the
  // centre, as a fraction of the half-frame, counts as the edge.
  edgeSide: 0.7,
  edgeOvershoot: 15,
};

function num(v, d) { return typeof v === "number" && isFinite(v) ? v : d; }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function wrap180(d) { return ((d + 180) % 360 + 360) % 360 - 180; }

// One face box ({x, y, width, height}, 0..1 of the frame) as the robot sees it: the bearing
// (deg, + left), the vertical offset (deg, + above centre), the distance (m) and where across
// the frame it sits (-1..1 from the centre, + left like the bearing).
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
  return { bearing: bearing, up: up, dist: dist, size: box.width, side: side };
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
  this.p = { az: wrap180(heading + r.bearing), dist: r.dist, raw: r.dist, size: r.size, vaz: 0, vdist: 0, t: t, up: r.up, side: num(r.side, 0) };
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
    raw: best.r.dist,
    size: best.r.size,
    vaz: clamp(0.5 * p.vaz + 0.5 * vaz, -90, 90),
    vdist: clamp(0.5 * p.vdist + 0.5 * vdist, -1.5, 1.5),
    t: t,
    up: best.r.up,
    side: num(best.r.side, 0),
  };
  this.seq++;
  return true;
};
Track.prototype.age = function (t) { return this.p ? t - this.p.t : Infinity; };

// The turn past where the person was last seen, toward the edge they were last seen at.
function edgePast(p, o) {
  var side = num(p.side, 0), over = num(o.edgeOvershoot, 0);
  return Math.abs(side) >= num(o.edgeSide, 1) ? (side > 0 ? over : -over) : 0;
}

// The follower: the person, as tracked, into growbot-drive.js's command. `set` is the distance
// to hold, taken from the person when following starts (owner, 2026-11-15). `stay` holds speed
// at 0 while it keeps turning and tilting to watch.
export function Follower(opts) {
  this.o = opts || FOLLOW;
  this.set = 1;
  this.pitch = 1;
  this.stay = false;
  this.seq = -1;
  this.reset();
}
// Forget the averaged distance: a reading from before a gap is stale once they're seen again.
Follower.prototype.reset = function () {
  this.dist = null;   // the averaged distance
  this.n = 0;         // readings in it
  this.heldMs = 0;    // how long its readings span
  this.lastT = null;
  this.walking = false;
  this.facing = true;
};
Follower.prototype.start = function (dist) {
  this.set = clamp(num(dist, 1), this.o.near, this.o.far);
  this.reset();
};
Follower.prototype.closer = function () { this.set = clamp(this.set - this.o.step, this.o.near, this.o.far); return this.set; };
Follower.prototype.further = function () { this.set = clamp(this.set + this.o.step, this.o.near, this.o.far); return this.set; };
// The command for now: the track (Track), the robot's heading (deg) and the time (ms).
// `state` says why: "follow", "hold" (in the band, on stay, or a stale reading), or "lost"
// (it turns toward where the person was last seen and stands: it never re-locks onto someone
// else by itself, the Track's match does that only for the same person).
Follower.prototype.command = function (track, heading, t) {
  var age = track.age(t), p = track.p;
  var turning = Math.max(this.pitch, this.o.pitchTurnMin);
  if (!p) { this.reset(); return { heading: 0, speed: 0, pitch: turning, state: "lost" }; }
  var past = edgePast(p, this.o);
  if (age > this.o.lostMs) {
    this.reset();
    return { heading: wrap180(p.az + past - heading), speed: 0, pitch: turning, state: "lost", past: past };
  }
  // Coast on the prediction only while readings are fresh: a stale one holds where they were
  // heading at that moment, so it never spins far on old speed (the step back when lost is at
  // most half a second of its turning), past it when they went over the frame's edge.
  var q = track.predict(Math.min(t, p.t + this.o.staleMs));
  var err = wrap180(q.az + (age > this.o.staleMs ? past : 0) - heading);
  // A stale reading forgets the average too, as the trained follower's tracker does after
  // half a second: a gap must not count toward the second of readings a walk waits for, nor
  // let the next reading after it stand in for the whole average.
  if (age > this.o.staleMs) this.reset();
  // Once per reading, the reading joins the distance's average (a plain mean until it spans
  // avgMs, then a moving one, so an early long reading weighs no more than any other).
  var newReading = track.seq !== this.seq;
  if (newReading) {
    this.seq = track.seq;
    var raw = num(p.raw, p.dist), gap = this.lastT === null ? 0 : Math.max(0, p.t - this.lastT);
    this.n++;
    this.heldMs += gap;
    this.lastT = p.t;
    var a = Math.max(1 / this.n, Math.min(1, gap / this.o.avgMs));
    this.dist = this.dist === null ? raw : this.dist + a * (raw - this.dist);
  }
  var speed = 0, state = "hold", off = num(this.dist, q.dist) - this.set;
  var fresh = age <= this.o.staleMs && !this.stay;
  var within = this.o.walkTurnMax + (this.walking ? 10 : 0);
  this.walking = fresh && (this.walking || this.heldMs >= this.o.walkWaitMs) &&
    off > (this.walking ? this.o.walkStop : this.o.walkStart) && Math.abs(err) <= within;
  if (this.walking) {
    // Full speed: growbot-drive.js walks only above its SPEED_ON, and the master trained
    // stop-or-go, so a speed scaled down near the set distance would never start the walk.
    speed = 1;
    state = "follow";
  } else if (fresh && off < -this.o.band) {
    speed = clamp(this.o.gain * (off + this.o.band), -this.o.backMax, 0);
    state = "follow";
    // Backing travels opposite the camera, so with the person well off to one side it would
    // close on them while "backing away": turn to face them first, then back.
    if (Math.abs(err) > this.o.backTurnMax) { speed = 0; state = "hold"; }
  }
  // The camera's height creeps toward the face once per reading, only while standing and
  // facing them: a walk's own stance pitches the lens, which would read as the face moving.
  var facing = this.facing = Math.abs(err) <= (this.facing ? this.o.pitchTurnMax : this.o.pitchFaceDeg);
  if (newReading && speed === 0 && facing && isFinite(p.up)) {
    this.pitch = clamp(this.pitch + this.o.pitchGain * p.up, -1, 1);
  }
  return { heading: err, speed: speed, pitch: facing ? this.pitch : turning, state: state, dist: num(this.dist, q.dist), set: this.set };
};
