// GrowBot's face finder: BlazeFace full-range on a crop that follows the face.
//
// WHY THIS EXISTS (owner, 2026-11-16, camera test run #223 on the robot's phone): the creature's
// own detector, BlazeFace short-range on a 320 px frame, saw a face 92% of the time at 0.7 m
// and 0% at 1 m, and past 2 m it locked onto something that wasn't a face. Full-range on the
// whole frame held to 2 m and fell to 5% at 2.5 m. Full-range on a crop about five face-widths
// square around where the face should be held 100% at every distance to 3 m and in the dark,
// at about 27 ms a frame. Its one weak spot was quick side steps (81%), where the crop fell
// behind the face while full-range on the whole frame held 99%: so a crop that misses is
// followed at once by a look at the whole frame, before the crop is tried again.
//
// One model call a frame, so it costs what the whole-frame detector costs. The worker that
// runs the creature's detector (v3/face-detector-worker.js, patched in apply.py) and the
// camera test (growbot-camtest.js) both use this file, so the test measures what ships.
//
// No `??` or `?.`: the creature's page runs on old iPhones (growbot_runner.js).

export var VISION = "/vendor/mediapipe/tasks-vision-0.10.35";
export var MODEL = "/vendor/mediapipe/models/blaze_face_full_range-float16-1.tflite";
// Every crop is drawn at one size (the model resizes to its own input anyway), so the canvas is
// never reallocated as the face's size changes.
export var CROP_PX = 288;

function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

// Square search tiles, each `frac` of the frame's short side, overlapping by 30%, covering it.
export function tiles(w, h, frac) {
  var side = Math.round(Math.min(w, h) * (frac || 0.6));
  function starts(len) {
    if (len <= side) return [0];
    var n = Math.ceil((len - side) / (side * 0.7)) + 1, out = [];
    for (var i = 0; i < n; i++) out.push(Math.round(((len - side) * i) / (n - 1)));
    return out;
  }
  var xs = starts(w), ys = starts(h), out = [];
  for (var j = 0; j < ys.length; j++) for (var i = 0; i < xs.length; i++) out.push({ x: xs[i], y: ys[j], w: side, h: side });
  return out;
}

// The planner. With a face in hand it crops around where the face should be now, so a far
// face fills the model's input as a near one does. Without one it alternates the whole frame
// (near faces) with the next search tile (far faces).
export function Zoom(opts) {
  var o = opts || {};
  this.span = o.span || 5;          // crop side, in face widths
  this.minSide = o.minSide || 160;  // px: under the model's 192 input there is nothing to gain
  this.keep = o.keep || 4;          // misses in a row before the face is let go
  this.lead = o.lead === undefined ? 0.15 : o.lead;   // s of movement the crop is widened by
  // A face under this many px of the model's input once the whole frame is shrunk to it can't
  // be seen there: a look after a miss is then a wider crop, not the whole frame.
  this.seenPx = o.seenPx || 12;
  this.track = null;                // {cx, cy, w, vx, vy, at} in frame px
  this.misses = 0;
  this.turn = 0;
  this.tiles = null;
  this.size = "";
}

Zoom.prototype.plan = function (w, h, at) {
  var size = w + "x" + h;
  if (size !== this.size) { this.size = size; this.tiles = tiles(w, h); this.track = null; this.misses = 0; }
  var t = this.track;
  if (t) {
    var dt = clamp((at - t.at) / 1000, 0, 0.25);
    var cx = t.cx + t.vx * dt, cy = t.cy + t.vy * dt, short = Math.min(w, h), side;
    if (this.misses % 2) {
      // Odd misses look wider: a face that moved faster than the crop's prediction is still
      // in view, and the next crop is centred on wherever it is found. A near face is looked
      // for in the whole frame; a far one, which the whole frame shrinks past seeing, in a
      // crop three times the usual about where it was.
      if ((t.w * 192) / Math.max(w, h) >= this.seenPx) return { x: 0, y: 0, w: w, h: h, why: "again" };
      side = Math.round(clamp(t.w * this.span * 3, this.minSide, short));
    } else {
      // A moving face gets a wider crop, by how far it could go in the next frame or so: a
      // crop sized for a still face loses one stepping quickly sideways.
      side = Math.round(clamp(t.w * this.span + 2 * this.lead * Math.hypot(t.vx, t.vy), this.minSide, short));
    }
    // A frame smaller than minSide (a camera that ignored the size asked for) crops to itself.
    side = Math.min(side, short);
    return {
      x: Math.round(clamp(cx - side / 2, 0, w - side)), y: Math.round(clamp(cy - side / 2, 0, h - side)),
      w: side, h: side, why: this.misses % 2 ? "wide" : "track",
    };
  }
  this.turn++;
  if (this.turn % 2) return { x: 0, y: 0, w: w, h: h, why: "frame" };
  var tile = this.tiles[(this.turn / 2 - 1) % this.tiles.length];
  return { x: tile.x, y: tile.y, w: tile.w, h: tile.h, why: "tile" };
};

// `boxes` in frame px. Picks the face nearest the predicted one (or the biggest, with none).
Zoom.prototype.update = function (boxes, at) {
  var t = this.track, best = null, bestScore = Infinity;
  for (var i = 0; i < boxes.length; i++) {
    var b = boxes[i], cx = b.x + b.w / 2, cy = b.y + b.h / 2;
    var s = t ? Math.hypot(cx - t.cx, cy - t.cy) / Math.max(t.w, 1) : -b.w;
    if (s < bestScore) { bestScore = s; best = b; }
  }
  // Another face, far from ours, is not a hit. A wider look after a miss is there to find a
  // face that moved further than the crop's prediction, so the one face it sees is taken
  // wherever it is; of several, the nearest within a wider gate.
  var again = t && this.misses % 2;
  if (t && best && bestScore > (again ? 6 : 3) && !(again && boxes.length === 1)) best = null;
  if (!best) {
    if (t && ++this.misses >= this.keep) { this.track = null; this.misses = 0; }
    return null;
  }
  var ncx = best.x + best.w / 2, ncy = best.y + best.h / 2;
  var vx = 0, vy = 0;
  if (t) {
    var dt = (at - t.at) / 1000;
    if (dt > 0.01 && dt < 0.5) { vx = 0.5 * t.vx + 0.5 * ((ncx - t.cx) / dt); vy = 0.5 * t.vy + 0.5 * ((ncy - t.cy) / dt); }
  }
  this.track = { cx: ncx, cy: ncy, w: best.w, vx: vx, vy: vy, at: at };
  this.misses = 0;
  return best;
};

// One frame through the planner and a MediaPipe FaceDetector in VIDEO mode. `frame` is anything
// it takes (an ImageBitmap, a canvas, a video) with its size in `w` and `h`; `canvas` is a
// CROP_PX square canvas to draw crops on. The boxes come back as fractions of the whole frame,
// with their scores, every face the model saw (a crop sees only the faces in it), the one the
// planner follows first; `face` is that one, null when it lost it this frame.
export function find(model, frame, w, h, at, zoom, canvas) {
  var c = zoom.plan(w, h, at), src = frame, scale = 1;
  if (c.w !== w || c.h !== h) {
    canvas.getContext("2d").drawImage(frame, c.x, c.y, c.w, c.h, 0, 0, canvas.width, canvas.height);
    src = canvas;
    scale = c.w / canvas.width;
  }
  var r = model.detectForVideo(src, at), px = [], boxes = [];
  ((r && r.detections) || []).forEach(function (d) {
    var b = d.boundingBox;
    if (!b) return;
    var p = { x: c.x + b.originX * scale, y: c.y + b.originY * scale, w: b.width * scale, h: b.height * scale };
    p.s = d.categories && d.categories[0] ? d.categories[0].score : null;
    px.push(p);
  });
  var face = zoom.update(px, at);
  if (face) px = [face].concat(px.filter(function (p) { return p !== face; }));
  px.forEach(function (p) {
    boxes.push({ x: p.x / w, y: p.y / h, width: p.w / w, height: p.h / h, score: p.s });
  });
  return { boxes: boxes, face: face ? { x: face.x / w, y: face.y / h, width: face.w / w, height: face.h / h, score: face.s } : null, crop: c };
}
