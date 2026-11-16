// GrowBot camera test: can the robot's phone see a face far enough, fast enough, for follow-me?
//
// WHY THIS EXISTS (owner, 2026-11-15, after the first real-robot follow trial): "a camera test
// where we kind of set up the phone and you do face tracking on me and make sure that it can keep
// up with what we are requiring from it. We might have to change apis" and "we want to be able to
// track faces at a further distance than straight up very close". The follower needs a face
// from 0.7 m to 2.5 m in a camera about 60° wide and 75° tall. The creature's detector is
// BlazeFace short-range on a 320x240 frame, a model made for selfies: on a portrait frame it
// needs the face to fill about a fifth of the frame's long side, which is about 0.6 m away.
//
// So this page props the phone up on its own (no legs), talks the person through stations
// (distances, sitting, edges, fast, dim) and runs four detectors on the very same frames:
//   now   the creature's detector as it is: short-range, frame scaled to 320 on its long side
//   full  BlazeFace full-range on the whole camera frame
//   zoom  full-range on a crop around the face it last saw, searching the frame in tiles when
//         it has none: the proposed tracker
//   body  BlazePose lite, the person rather than the face: a fallback for when the face is out
//         of frame (standing close) or too small
// It logs every frame to the box through the body test's log (`/api/box/bodytest`, run id
// `bt-cam-…`), so a run can be read back from the debug API, and shows a verdict per detector.
//
// The pure parts (stations, geometry, the zoom planner, the verdicts) are exported and run in
// Node (backend/tests/unit/test_growbot_camtest.py); the page boots only on camtest.html.
//
// No `??` or `?.`: the robot's phone may be an old iPhone (growbot_runner.js).

export var FOV = { narrow: 60, wide: 75 };   // degrees across the frame's short and long side
export var NEED = { near: 0.7, far: 2.5, rate: 0.9, fps: 10 };
// The width BlazeFace's box spans on an adult face, cheek to cheek, before any calibration.
export var FACE_M = 0.15;
export var PIPES = ["now", "full", "zoom", "body"];
export var PIPE_NAMES = {
  now: "Now · short-range, 320 px",
  full: "Full-range, whole frame",
  zoom: "Full-range + zoom crop",
  body: "Body (pose)",
};

export var PREP_S = 7;
export var STATIONS = [
  { id: "d07", title: "Sit 0.7 m away", say: "Sit about 0.7 metres from the phone, facing it.", m: 0.7, secs: 10 },
  { id: "d10", title: "Stand 1 m away", say: "Stand up, 1 metre from the phone.", m: 1, secs: 10 },
  { id: "d15", title: "Stand 1.5 m away", say: "Step back to 1.5 metres.", m: 1.5, secs: 10 },
  { id: "d20", title: "Stand 2 m away", say: "Step back to 2 metres.", m: 2, secs: 10 },
  { id: "d25", title: "Stand 2.5 m away", say: "Step back to 2.5 metres.", m: 2.5, secs: 10 },
  { id: "d30", title: "Stand 3 m away", say: "Step back to 3 metres.", m: 3, secs: 10 },
  { id: "sit20", title: "Sit 2 m away", say: "Sit down about 2 metres away.", m: 2, secs: 10 },
  { id: "edges", title: "Walk slowly sideways, out of view each side", say: "At about 1.5 metres, walk slowly sideways until you leave the view, then all the way across and out the other side, then back to the middle.", m: 1.5, secs: 25, kind: "edges" },
  { id: "fast", title: "Step quickly side to side", say: "Stay about 1.5 metres away and step quickly side to side, staying in view.", m: 1.5, secs: 12, kind: "fast" },
  { id: "away", title: "Walk slowly back from 0.5 m to 3 m", say: "Come close, about half a metre. When I say go, walk slowly straight back to 3 metres or more.", secs: 20, kind: "range" },
  { id: "dim", title: "Main light off, 1.5 m", say: "Turn off the main light, then stand 1.5 metres away.", m: 1.5, secs: 12, kind: "light" },
];

export function runId(now) {
  return "bt-cam-" + Math.floor(now).toString(36).slice(-16);
}

// The frame's horizontal field of view: a portrait frame's width is the camera's short side.
export function hfov(w, h, fov) {
  var f = fov || FOV;
  return w <= h ? f.narrow : f.wide;
}

// Where a point across the frame is, in degrees off the camera's axis (left negative).
export function angleOf(xFrac, hdeg) {
  var t = Math.tan((hdeg * Math.PI) / 360);
  return (Math.atan((xFrac - 0.5) * 2 * t) * 180) / Math.PI;
}

// How far a face is from its box's width (a fraction of the frame width). With a calibration
// (`k` = the box width fraction seen at 1 m) the small-angle model is fitted to this person.
export function distanceOf(wFrac, hdeg, k) {
  if (!(wFrac > 0)) return null;
  if (k > 0) return k / wFrac;
  var half = (wFrac * hdeg * Math.PI) / 360;
  return FACE_M / (2 * Math.tan(half));
}

function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

export function median(xs) {
  if (!xs.length) return null;
  var s = xs.slice().sort(function (a, b) { return a - b; });
  var m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

export function pct(xs, p) {
  if (!xs.length) return null;
  var s = xs.slice().sort(function (a, b) { return a - b; });
  return s[Math.min(s.length - 1, Math.floor(p * s.length))];
}

// Square search tiles over a W×H frame, each `frac` of its short side, overlapping, so a face
// too small for the whole-frame pass fills enough of one tile's model input to be found.
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

// The proposed tracker's planner: one model call a frame, so it costs what `full` costs.
// With a face in hand it crops around where the face should be now, about five face-widths
// square, so a far face fills the model's input as a near one does. Without one it alternates
// the whole frame (near faces) with the next search tile (far faces).
export function Zoom(opts) {
  var o = opts || {};
  this.span = o.span || 5;          // crop side, in face widths
  this.minSide = o.minSide || 160;  // px: under the model's 192 input there is nothing to gain
  this.keep = o.keep || 4;          // misses in a row before the face is let go
  this.track = null;                // {cx, cy, w, vx, vy, at} in frame px
  this.misses = 0;
  this.turn = 0;
  this.tiles = null;
  this.size = "";
}

Zoom.prototype.plan = function (w, h, at) {
  var size = w + "x" + h;
  if (size !== this.size) { this.size = size; this.tiles = tiles(w, h); this.track = null; }
  var t = this.track;
  if (t) {
    var dt = clamp((at - t.at) / 1000, 0, 0.25);
    var cx = t.cx + t.vx * dt, cy = t.cy + t.vy * dt;
    var side = Math.round(clamp(t.w * this.span, this.minSide, Math.min(w, h)));
    return { x: Math.round(clamp(cx - side / 2, 0, w - side)), y: Math.round(clamp(cy - side / 2, 0, h - side)), w: side, h: side, why: "track" };
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
  if (t && best && bestScore > 3) best = null;   // another face, far from ours: not a hit
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

// One frame's row as it is logged: per detector `[x, y, w, h, score, ms]` (fractions of the
// frame, 3 places) on a hit, `[ms]` on a miss, absent when it didn't run.
export function hitOf(cell) { return !!(cell && cell.length > 1); }

// What one station's measuring frames say, per detector. `k` is a calibration (one number, or
// one per detector) for distanceOf.
export function stationStats(rows, hdeg, k) {
  var out = {};
  PIPES.forEach(function (p) {
    var n = 0, hits = 0, ms = [], wf = [], dist = [], ang = [], losses = [], run = 0, last = null, speeds = [], prev = null;
    rows.forEach(function (r) {
      var c = r[p];
      if (!c) return;
      n++;
      ms.push(c[c.length - 1]);
      if (hitOf(c)) {
        hits++;
        var a = angleOf(c[0] + c[2] / 2, hdeg);
        ang.push(a);
        if (p !== "body") { wf.push(c[2]); var d = distanceOf(c[2], hdeg, k && typeof k === "object" ? k[p] : k); if (d) dist.push(d); }
        if (prev && r.t - prev.t > 0 && r.t - prev.t < 300) speeds.push(Math.abs(a - prev.a) / ((r.t - prev.t) / 1000));
        prev = { a: a, t: r.t };
        last = a; run = 0;
      } else {
        prev = null;
        if (++run === 3 && last !== null) losses.push(last);
      }
    });
    if (!n) return;
    var r1 = function (v) { return v === null ? null : Math.round(v * 10) / 10; };
    out[p] = {
      frames: n, rate: Math.round((hits / n) * 100) / 100, ms: r1(median(ms)), ms90: r1(pct(ms, 0.9)),
      width: wf.length ? Math.round(median(wf) * 1000) / 1000 : null,
      dist: r1(median(dist)), distMax: r1(pct(dist, 0.95)),
      left: r1(ang.length ? Math.min.apply(null, ang) : null), right: r1(ang.length ? Math.max.apply(null, ang) : null),
      losses: losses.slice(0, 10).map(r1), lossCount: losses.length, speed: r1(pct(speeds, 0.9)),
    };
  });
  return out;
}

// The verdicts for the whole run. `stations` maps a station id to its stationStats; `meta` has
// the frame's horizontal FOV.
export function verdicts(stations, meta) {
  var half = meta.hdeg / 2, res = {}, lines = {};
  PIPES.forEach(function (p) {
    var reach = 0, nearOk = null, worst = null, msAll = [];
    STATIONS.forEach(function (s) {
      var st = stations[s.id] && stations[s.id][p];
      if (!st) return;
      msAll.push(st.ms);
      if (s.m && !s.kind && s.id.charAt(0) === "d") {
        if (st.rate >= NEED.rate && s.m > reach) reach = s.m;
        if (s.m >= NEED.near && s.m <= NEED.far && (worst === null || st.rate < worst.rate)) worst = { m: s.m, rate: st.rate };
        if (s.m === 1) nearOk = st.rate;
      }
    });
    var away = stations.away && stations.away[p];
    if (away && away.distMax && p !== "body" && away.distMax > reach && away.rate >= 0.5) reach = Math.max(reach, Math.min(away.distMax, 6));
    var ms = median(msAll.filter(function (v) { return v !== null; }));
    var edges = stations.edges && stations.edges[p];
    var fps = ms ? Math.round(1000 / ms) : null;
    var why = [];
    if (reach) why.push("sees you to " + reach + " m");
    else why.push("never held 90% at any distance");
    if (worst) why.push("worst " + Math.round(worst.rate * 100) + "% at " + worst.m + " m");
    if (ms !== null) why.push(ms + " ms a frame (~" + fps + "/s)");
    if (edges && edges.left !== null) why.push("tracked " + Math.round(edges.left) + "° to +" + Math.round(edges.right) + "° of ±" + Math.round(half) + "°");
    var v = "fail";
    if (p === "body") v = reach >= NEED.far ? "ok" : reach ? "warn" : "fail";
    else if (reach >= NEED.far && (!worst || worst.rate >= NEED.rate)) v = "ok";
    else if (reach >= 1.5) v = "warn";
    if (v === "ok" && fps !== null && fps < NEED.fps) v = "warn";
    res[p] = { v: v, why: PIPE_NAMES[p] + ": " + why.join(", ") };
    lines[p] = { reach: reach, worst: worst, ms: ms, fps: fps, near: nearOk, edges: edges ? [edges.left, edges.right] : null };
  });
  var dim = stations.dim || {};
  var light = {};
  PIPES.forEach(function (p) { if (dim[p]) light[p] = dim[p].rate; });
  return { results: res, numbers: lines, light: light };
}

// ---------------------------------------------------------------------------------------------
// The page. Everything below needs a browser; nothing above does.

var VISION = "/vendor/mediapipe/tasks-vision-0.10.35";
var MODELS = {
  short: "/vendor/mediapipe/models/blaze_face_short_range-float16-1.tflite",
  full: "/vendor/mediapipe/models/blaze_face_full_range-float16-1.tflite",
  pose: "/vendor/mediapipe/models/pose_landmarker_lite-float16-1.task",
};
var COLORS = { now: "#9aa7b8", full: "#5aa2ff", zoom: "#37e0c8", body: "#ffb347" };
var RESOLUTIONS = { "640": [640, 480], "960": [960, 720], "1280": [1280, 960] };

function boot() {
  var $ = function (id) { return document.getElementById(id); };
  var qp = new URLSearchParams(location.search);
  var robot = (qp.get("id") || (function () { try { return localStorage.getItem("gb_walk_id") || ""; } catch (e) { return ""; } })()).trim();
  if (!/^gb-[A-Za-z0-9]{1,32}$/.test(robot)) robot = "";
  var video = $("vid"), ov = $("ov"), g = ov.getContext("2d");
  var frameCanvas = document.createElement("canvas"), fctx = frameCanvas.getContext("2d", { willReadFrequently: true });
  var nowCanvas = document.createElement("canvas"), nctx = nowCanvas.getContext("2d");
  var cropCanvas = document.createElement("canvas"), cctx = cropCanvas.getContext("2d");
  var lumaCanvas = document.createElement("canvas"), lctx = lumaCanvas.getContext("2d", { willReadFrequently: true });
  lumaCanvas.width = 32; lumaCanvas.height = 24;
  var det = {}, delegate = {}, on = { now: true, full: true, zoom: true, body: true };
  var zoom = new Zoom(), stream = null, busy = false, running = false, lastCrop = null, last = {};
  var rows = [], pending = [], all = [], seq = 0, run = null, saved = robot ? "saving" : "not saved: no robot on this phone";
  var t0 = 0, phase = "idle", stIdx = -1, stRows = [], stats = {}, stopAt = 0, cal = {}, hdeg = FOV.narrow;
  var prevAt = 0, frames = 0, fpsAt = 0, fps = 0, ua = String(navigator.userAgent || "").slice(0, 120), info = {};

  function say(text) {
    try { if (window.speechSynthesis) { window.speechSynthesis.cancel(); window.speechSynthesis.speak(new SpeechSynthesisUtterance(text)); } } catch (e) {}
  }
  function show(title, sub) { $("title").textContent = title; $("sub").textContent = sub || ""; }

  function loadVision() {
    return import(VISION + "/vision_bundle.mjs").then(function (mod) {
      return mod.FilesetResolver.forVisionTasks(VISION + "/wasm").then(function (fs) {
        function make(name, create) {
          function tryOn(dev) {
            return create(dev).then(function (t) { det[name] = t; delegate[name] = dev; });
          }
          return tryOn("GPU").catch(function () { return tryOn("CPU"); }).catch(function (e) {
            delegate[name] = "failed: " + String((e && e.message) || e).slice(0, 80);
          });
        }
        function face(model) {
          return function (dev) {
            return mod.FaceDetector.createFromOptions(fs, { baseOptions: { modelAssetPath: model, delegate: dev }, runningMode: "IMAGE", minDetectionConfidence: 0.5 });
          };
        }
        // One after another: the wasm loader is shared, and a phone's GPU likes one compile at a time.
        return make("now", face(MODELS.short))
          .then(function () { return make("full", face(MODELS.full)); })
          .then(function () { return make("zoom", face(MODELS.full)); })
          .then(function () {
            return make("body", function (dev) {
              return mod.PoseLandmarker.createFromOptions(fs, { baseOptions: { modelAssetPath: MODELS.pose, delegate: dev }, runningMode: "VIDEO", numPoses: 1 });
            });
          });
      });
    });
  }

  function openCamera() {
    var res = RESOLUTIONS[$("res").value] || RESOLUTIONS["960"];
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    return navigator.mediaDevices.getUserMedia({
      video: { facingMode: $("lens").value, width: { ideal: res[0] }, height: { ideal: res[1] } }, audio: false,
    }).then(function (s) {
      stream = s; video.srcObject = s;
      return video.play().catch(function () {});
    }).then(function () {
      var until = performance.now() + 8000;
      return new Promise(function (ok, no) {
        (function wait() {
          if (video.videoWidth) ok();
          else if (performance.now() > until) no(new Error("the camera opened but sent no picture"));
          else setTimeout(wait, 50);
        })();
      });
    }).then(function () {
      var st = {};
      try { st = stream.getVideoTracks()[0].getSettings() || {}; } catch (e) {}
      info = { w: video.videoWidth, h: video.videoHeight, fps: st.frameRate || null, facing: st.facingMode || "" };
      $("vid").style.transform = $("lens").value === "user" ? "scaleX(-1)" : "none";
      $("ov").style.transform = $("vid").style.transform;
    });
  }

  function detectFace(name, src, sw, sh, ox, oy, scale) {
    var t = performance.now(), r = det[name].detect(src), ms = performance.now() - t, boxes = [];
    (r.detections || []).forEach(function (d) {
      var b = d.boundingBox, s = d.categories && d.categories[0] ? d.categories[0].score : 0;
      // Keypoints 0 and 1 are the eyes: their spacing is a second distance read, less
      // sensitive to how loosely the box hugs the face, and their tilt says the head's roll.
      var k = d.keypoints || [], eye = null;
      if (k.length > 1) eye = [ox + k[0].x * sw * scale, oy + k[0].y * sh * scale, ox + k[1].x * sw * scale, oy + k[1].y * sh * scale];
      boxes.push({ x: ox + b.originX * scale, y: oy + b.originY * scale, w: b.width * scale, h: b.height * scale, s: s, eye: eye });
    });
    return { boxes: boxes, ms: ms };
  }

  function cell(box, w, h, ms) {
    var r3 = function (v) { return Math.round(v * 1000) / 1000; };
    var m1 = Math.round(ms * 10) / 10;
    return box ? [r3(box.x / w), r3(box.y / h), r3(box.w / w), r3(box.h / h), Math.round((box.s || 1) * 100) / 100, m1] : [m1];
  }
  // The verbose part (owner, 2026-11-16: "increase more verbose logging for anything that you
  // need ... to make good decisions in the future"): how many faces each detector saw, the
  // chosen face's eye spacing (fraction of frame width) and roll, and the second-best score.
  function extra(row, p, boxes, chosen, w, h) {
    row[p + "N"] = boxes.length;
    if (chosen && chosen.eye) {
      var e = chosen.eye, dx = e[2] - e[0], dy = e[3] - e[1];
      row[p + "E"] = [Math.round((Math.hypot(dx, dy) / w) * 10000) / 10000, Math.round((Math.atan2(dy, dx) * 180) / Math.PI)];
    }
    if (boxes.length > 1) row[p + "2"] = Math.round(boxes.map(function (b) { return b.s; }).sort(function (a, b) { return b - a; })[1] * 100) / 100;
  }
  function biggest(bs) { var b = null; bs.forEach(function (x) { if (!b || x.w > b.w) b = x; }); return b; }

  function step(at) {
    var w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return;
    hdeg = hfov(w, h, { narrow: +$("fovN").value || FOV.narrow, wide: +$("fovW").value || FOV.wide });
    if (frameCanvas.width !== w || frameCanvas.height !== h) { frameCanvas.width = w; frameCanvas.height = h; }
    fctx.drawImage(video, 0, 0, w, h);
    var row = { t: Math.round(at - t0), dt: Math.round(at - prevAt) };
    prevAt = at;
    lctx.drawImage(frameCanvas, 0, 0, 32, 24);
    var px = lctx.getImageData(0, 0, 32, 24).data, sum = 0;
    for (var i = 0; i < px.length; i += 4) sum += px[i] * 0.3 + px[i + 1] * 0.59 + px[i + 2] * 0.11;
    row.l = Math.round(sum / (px.length / 4));
    last = {};
    if (on.now && det.now) {
      // The creature's own frame: the camera's at 320 on its long side, as next.html asks.
      var s = 320 / Math.max(w, h), nw = Math.round(w * s), nh = Math.round(h * s);
      if (nowCanvas.width !== nw || nowCanvas.height !== nh) { nowCanvas.width = nw; nowCanvas.height = nh; }
      nctx.drawImage(frameCanvas, 0, 0, nw, nh);
      var a = detectFace("now", nowCanvas, nw, nh, 0, 0, 1 / s), b1 = biggest(a.boxes);
      row.now = cell(b1, w, h, a.ms); last.now = b1; extra(row, "now", a.boxes, b1, w, h);
    }
    if (on.full && det.full) {
      var f = detectFace("full", frameCanvas, w, h, 0, 0, 1), b2 = biggest(f.boxes);
      row.full = cell(b2, w, h, f.ms); last.full = b2; extra(row, "full", f.boxes, b2, w, h);
    }
    if (on.zoom && det.zoom) {
      var c = zoom.plan(w, h, at);
      // Every crop is drawn at one size (the model resizes to its 192 input anyway), so the
      // canvas is never reallocated as the face's size changes.
      var side = 288, z;
      var t1 = performance.now();
      if (c.why === "frame") z = detectFace("zoom", frameCanvas, w, h, 0, 0, 1);
      else {
        if (cropCanvas.width !== side) { cropCanvas.width = side; cropCanvas.height = side; }
        cctx.drawImage(frameCanvas, c.x, c.y, c.w, c.h, 0, 0, side, side);
        z = detectFace("zoom", cropCanvas, side, side, c.x, c.y, c.w / side);
      }
      var b3 = zoom.update(z.boxes, at);
      row.zoom = cell(b3, w, h, performance.now() - t1); row.zc = c.why.charAt(0); extra(row, "zoom", z.boxes, b3, w, h);
      row.zr = [c.x / w, c.y / h, c.w / w, c.h / h].map(function (v) { return Math.round(v * 1000) / 1000; });
      last.zoom = b3; lastCrop = c;
    }
    if (on.body && det.body) {
      var t2 = performance.now(), pr = det.body.detectForVideo(frameCanvas, at), pms = performance.now() - t2;
      var lm = pr.landmarks && pr.landmarks[0], pb = null;
      if (lm) {
        var x0 = 1, y0 = 1, x1 = 0, y1 = 0;
        lm.forEach(function (p) { if ((p.visibility || 0) > 0.5) { x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y); } });
        var nose = lm[0];
        if (x1 > x0) pb = { x: x0 * w, y: y0 * h, w: (x1 - x0) * w, h: (y1 - y0) * h, s: 1 };
        // Head out of frame (standing close): the nose's place, wherever the model puts it.
        // Shoulder span: the body's own distance read when the face is too small or out of frame.
        var ls = lm[11], rs = lm[12];
        if (ls && rs) row.sh = [Math.round(Math.hypot((ls.x - rs.x) * w, (ls.y - rs.y) * h) / w * 1000) / 1000, Math.round(Math.min(ls.visibility || 0, rs.visibility || 0) * 100) / 100];
        row.nose = [Math.round(nose.x * 1000) / 1000, Math.round(nose.y * 1000) / 1000, Math.round((nose.visibility || 0) * 100) / 100];
      }
      row.body = cell(pb, w, h, pms); last.body = pb;
    }
    record(row);
    draw(w, h);
  }

  function draw(w, h) {
    var cw = ov.clientWidth, ch = ov.clientHeight;
    if (ov.width !== cw) ov.width = cw;
    if (ov.height !== ch) ov.height = ch;
    g.clearRect(0, 0, cw, ch);
    var s = Math.min(cw / w, ch / h), ox = (cw - w * s) / 2, oy = (ch - h * s) / 2;
    if (lastCrop && lastCrop.why !== "frame") {
      g.setLineDash([6, 6]); g.strokeStyle = "rgba(55,224,200,.5)"; g.lineWidth = 1;
      g.strokeRect(ox + lastCrop.x * s, oy + lastCrop.y * s, lastCrop.w * s, lastCrop.h * s); g.setLineDash([]);
    }
    PIPES.forEach(function (p) {
      var b = last[p];
      if (!b) return;
      g.strokeStyle = COLORS[p]; g.lineWidth = p === "zoom" ? 4 : 2;
      g.strokeRect(ox + b.x * s, oy + b.y * s, b.w * s, b.h * s);
    });
    var chips = PIPES.map(function (p) {
      var r = rows.length ? rows[rows.length - 1][p] : null;
      if (!r) return "";
      var hit = hitOf(r);
      var d = hit && p !== "body" ? distanceOf(r[2], hdeg, cal[p]) : null;
      return '<span class="chip' + (hit ? " hit" : "") + '" style="--c:' + COLORS[p] + '">' + p + " " + (hit ? "●" : "○") + " " + Math.round(r[r.length - 1]) + "ms" + (d ? " · " + d.toFixed(1) + "m" : "") + "</span>";
    }).join("");
    $("chips").innerHTML = chips + '<span class="chip">' + fps + "/s · " + w + "×" + h + " · " + Math.round(hdeg) + "°</span>";
  }

  function record(row) {
    frames++;
    var now = performance.now();
    if (now - fpsAt > 1000) { fps = frames; frames = 0; fpsAt = now; }
    if (phase === "measure") { row.st = STATIONS[stIdx].id; stRows.push(row); }
    if (run && phase !== "result") { pending.push(row); all.push(row); rows.push(row); if (rows.length > 4) rows.shift(); }
    else { rows = [row]; }
  }

  function loop() {
    if (!running) return;
    var schedule = video.requestVideoFrameCallback ? function (f) { video.requestVideoFrameCallback(f); } : function (f) { requestAnimationFrame(f); };
    schedule(function () {
      if (!busy) {
        busy = true;
        try { step(performance.now()); } catch (e) { event({ e: "error", why: String((e && e.message) || e).slice(0, 120) }); }
        busy = false;
      }
      loop();
    });
  }

  function event(ev) {
    ev.t = Math.round(performance.now() - t0);
    if (run) { pending.push(ev); all.push(ev); }
  }

  // The body test's log, so a run reads back the same way (`bt-cam-…`): chunks go one at a
  // time, in order, each waiting for the one before, and a refused chunk's rows go back first.
  var chain = Promise.resolve();
  function post(status) {
    if (!run || !robot) return chain;
    chain = chain.then(function () {
      var chunk = { robot: robot, run: run, seq: seq, status: status, summary: summary(status), rows: pending.splice(0, 400) };
      return fetch("/api/box/bodytest", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(chunk) })
        .then(function (r) {
          if (r.ok) { seq++; saved = "saved to the box as " + run; return true; }
          pending = chunk.rows.concat(pending);
          saved = r.status === 403 ? "not saved: this phone isn't signed in to drive " + robot : "not saved yet (box said " + r.status + ")";
          return false;
        })
        .catch(function () { pending = chunk.rows.concat(pending); saved = "not saved yet (offline)"; return false; })
        .then(function (ok) { $("saved").textContent = saved; return ok; });
    });
    return chain;
  }

  function summary(status) {
    var v = verdicts(stats, { hdeg: hdeg });
    var st = STATIONS[stIdx];
    return {
      kind: "camera", phase: status === "running" ? "run" : "result", step: st ? st.id : "", label: st ? st.title : "",
      secs: Math.round((performance.now() - t0) / 1000), results: v.results, numbers: v.numbers, light: v.light,
      stations: stats, camera: info, hdeg: hdeg, cal: cal, delegate: delegate, ua: ua,
    };
  }

  function sleep(ms) { return new Promise(function (ok) { setTimeout(ok, ms); }); }

  function countdown(secs, label) {
    var end = performance.now() + secs * 1000;
    skip = false;   // a Skip tapped late in one countdown never carries into the next
    return new Promise(function (ok) {
      (function tick() {
        if (phase === "stopped") return ok();
        var left = Math.ceil((end - performance.now()) / 1000);
        $("count").textContent = label + " " + Math.max(0, left);
        if (left <= 0 || skip) { skip = false; ok(); } else setTimeout(tick, 200);
      })();
    });
  }
  var skip = false;

  function guided() {
    run = runId(Date.now()); seq = 0; pending = []; all = []; stats = {}; cal = {}; t0 = performance.now();
    event({ e: "start", camera: info, delegate: delegate, robot: robot });
    $("results").innerHTML = ""; $("stopBtn").textContent = "Stop";
    var poster = setInterval(function () { post("running"); }, 2000);
    var chain = Promise.resolve();
    STATIONS.forEach(function (s, i) {
      chain = chain.then(function () {
        if (phase === "stopped") return;
        stIdx = i; phase = "prep";
        show((i + 1) + "/" + STATIONS.length + " · " + s.title, s.say);
        say(s.say);
        event({ e: "station", id: s.id });
        return countdown(PREP_S + (s.kind === "light" ? 6 : 0), "get ready").then(function () {
          if (phase === "stopped") return;
          phase = "measure"; stRows = [];
          say(s.kind === "range" ? "Go. Walk slowly back." : "Hold.");
          return countdown(s.secs, "measuring").then(function () {
            // Each model's box hugs a face its own way, so each is calibrated on its own 1 m.
            if (s.id === "d10") {
              var one = stationStats(stRows, hdeg, {});
              PIPES.forEach(function (p) { if (one[p] && one[p].width && one[p].rate > 0.5) cal[p] = one[p].width; });
            }
            stats[s.id] = stationStats(stRows, hdeg, cal);
            event({ e: "measured", id: s.id, stats: stats[s.id] });
          });
        });
      });
    });
    return chain.then(function () {
      clearInterval(poster);
      var status = phase === "stopped" ? "stopped" : "done";
      phase = "result";
      // Nothing left to measure: no detectors or camera running on a phone left propped up.
      running = false;
      try { if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
      say(status === "done" ? "Done. The results are on the screen." : "Stopped.");
      show(status === "done" ? "Camera test done" : "Camera test stopped", "");
      $("count").textContent = "";
      paintResults();
      // The last rows, then the final status. A box that refuses stops it: Download keeps them.
      // Only the very last chunk carries the end status: the box closes a run on it and refuses
      // what comes after.
      function flush(tries) { return post(pending.length > 400 ? "running" : status).then(function (ok) { if (ok && pending.length && tries < 200) return flush(tries + 1); }); }
      return flush(0);
    });
  }

  function paintResults() {
    var v = verdicts(stats, { hdeg: hdeg }), html = "<h3>Verdicts</h3>";
    PIPES.forEach(function (p) {
      var r = v.results[p];
      html += '<div class="v ' + r.v + '"><b>' + r.v.toUpperCase() + "</b> " + r.why + "</div>";
    });
    html += "<h3>Detection rate by station</h3><table><tr><th></th>" + PIPES.map(function (p) { return "<th>" + p + "</th>"; }).join("") + "</tr>";
    STATIONS.forEach(function (s) {
      var st = stats[s.id];
      if (!st) return;
      html += "<tr><td>" + s.title + "</td>" + PIPES.map(function (p) {
        var x = st[p];
        if (!x) return "<td>–</td>";
        var extra = x.dist ? " · " + x.dist + "m" : "";
        return "<td>" + Math.round(x.rate * 100) + "%" + extra + "</td>";
      }).join("") + "</tr>";
    });
    html += "</table><p class=mut>Camera " + info.w + "×" + info.h + ", " + Math.round(hdeg) + "° wide. Distance " + (Object.keys(cal).length ? "calibrated per detector at 1 m (" + Object.keys(cal).join(", ") + ")" : "from a 15 cm face") + ". Detectors ran on: " + PIPES.map(function (p) { return p + " " + (delegate[p] || "off"); }).join(", ") + ". " + saved + ".</p>";
    $("results").innerHTML = html;
    $("dl").style.display = "inline-block";
  }

  $("dl").onclick = function () {
    // The whole log, every frame, whether or not the box kept it.
    var blob = new Blob([JSON.stringify({ run: run, summary: summary("done"), rows: all })], { type: "application/json" });
    var a = document.createElement("a"), url = URL.createObjectURL(blob);
    a.href = url; a.download = (run || "camtest") + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
  };
  ["now", "full", "zoom", "body"].forEach(function (p) {
    $("on_" + p).onchange = function () { on[p] = $("on_" + p).checked; };
  });
  $("res").onchange = $("lens").onchange = function () {
    if (running) openCamera().catch(function (e) { show("The camera didn't reopen", String((e && e.message) || e)); });
  };
  $("skipBtn").onclick = function () { skip = true; };
  $("stopBtn").onclick = function () {
    if (phase === "prep" || phase === "measure" || phase === "starting") { phase = "stopped"; skip = true; event({ e: "stop" }); }
  };
  $("startBtn").onclick = function () {
    $("startBtn").disabled = true;
    show("Loading the detectors…", "First time can take a few seconds.");
    say("Camera test. Prop the phone up where the robot's camera would be, then step away.");
    var wake = null;
    try { if (navigator.wakeLock) navigator.wakeLock.request("screen").then(function (l) { wake = l; }).catch(function () {}); } catch (e) {}
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      show("No camera here", "This page needs the box's https address to use the camera.");
      $("startBtn").disabled = false;
      return;
    }
    openCamera().then(loadVision).then(function () {
      if (!Object.keys(det).length) throw new Error("no detector loaded: " + JSON.stringify(delegate));
      running = true; loop();
      $("startBtn").style.display = "none"; $("live").style.display = "flex";
      show("Prop the phone up", "Where the robot's camera would be, screen toward you. The test starts in a moment: listen for each station.");
      phase = "starting";
      return countdown(10, "starting in");
    }).then(function () {
      // A Stop during the lead-in still runs the flow, which ends at once as "stopped".
      return guided();
    }).catch(function (e) {
      show("The camera test couldn't start", String((e && e.message) || e));
      $("startBtn").disabled = false;
    });
  };
  $("saved").textContent = robot ? "Logs go to the box for " + robot + "." : "No robot on this phone: results stay here (Download).";
}

if (typeof document !== "undefined" && document.getElementById && document.getElementById("camtest")) {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
}
