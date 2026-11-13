/* The creature's eyes follow the face in front of it, on the phone, with no model call
   (owner, 2026-11-12). The face box comes from the BlazeFace detector the leg tracker already
   ships (/v3/agent-face-track.mjs): while that tracker is on it hands its boxes here (apply.py
   hooks both of its result paths), and while it is off (no robot paired, a person said stop,
   the camera or detector failed it) this file runs its own copy of the same detector, in a
   worker, on the creature's open front camera. Never both at once: a second GPU worker could
   make the tracker's frame miss its 350 ms deadline, which drops leg tracking to the main
   thread for good. Frames never leave the browser: the worker gets a bitmap and answers with
   boxes. */
(function () {
  "use strict";

  var TICK_MS = 50;
  var RATE_MS = 125, LOW_POWER_RATE_MS = 250, SEARCH_RATE_MS = 500;
  /* A tracker that stopped handing boxes over this long ago is not watching, enabled or not. */
  var FED_MS = 1500;
  var SEARCH_AFTER_MS = 5000, CLOSE_AFTER_MS = 30000;
  /* A face rarely reaches the frame's edge, so a little gain lets the pupils use their range;
     side to side it is double the old 1.6, since a face barely crosses the narrow front
     camera's frame (owner, 2026-11-13: left to right gain needs like doubled). */
  var GAIN_X = 3.2, GAIN_Y = 1.2;
  var RETRY_MS = [30000, 120000];

  var model = null, loading = false, fails = 0, retryAt = 0;
  var lastFed = -Infinity, lastAsk = -Infinity, lastFace = -Infinity, lastUse = -Infinity;
  var previous = null;

  function now() { return performance.now(); }

  /* The biggest face is the nearest one; once one is followed, the box closest to it, so two
     people in frame do not make the eyes flick between them. */
  function pick(boxes) {
    var ok = (boxes || []).filter(function (b) {
      return b && isFinite(b.x) && isFinite(b.y) && b.width > 0 && b.height > 0;
    });
    if (!ok.length) return null;
    function centre(b) { return { x: b.x + b.width / 2, y: b.y + b.height / 2 }; }
    if (previous && ok.length > 1) {
      var p = centre(previous);
      ok.sort(function (a, b) {
        var ca = centre(a), cb = centre(b);
        return Math.hypot(ca.x - p.x, ca.y - p.y) - Math.hypot(cb.x - p.x, cb.y - p.y);
      });
    } else {
      ok.sort(function (a, b) { return b.width * b.height - a.width * a.height; });
    }
    return ok[0];
  }

  /* The front camera's frames are not mirrored: someone to the screen's right is on the
     frame's left, so x flips and y does not. */
  function toScreen(b) {
    var cx = b.x + b.width / 2, cy = b.y + b.height / 2;
    return { x: -(cx - 0.5) * 2 * GAIN_X, y: (cy - 0.5) * 2 * GAIN_Y };
  }

  function feed(boxes) {
    var b = pick(boxes);
    previous = b;
    if (!b) return;
    lastFace = now();
    var at = toScreen(b);
    if (window.gbFace && window.gbFace.see) window.gbFace.see(at.x, at.y);
  }

  function trackerWatching(t) {
    var ft = window._faceTrack;
    return !!(ft && ft.enabled) || t - lastFed < FED_MS;
  }

  function camera() {
    var v = window.camV;
    if (!window.camOn || window.camFacing !== "user" || !v || v.readyState < 2 || v.paused) return null;
    if (document.hidden || window.paused || window.dreaming) return null;
    return v;
  }

  function close() {
    if (model) { try { model.close(); } catch (e) {} }
    model = null;
  }

  function failed(why) {
    close();
    fails++;
    retryAt = fails > RETRY_MS.length ? Infinity : now() + RETRY_MS[fails - 1];
    try { if (typeof window.logEv === "function") window.logEv("eyes_detector", { why: why, fails: fails }); } catch (e) {}
  }

  /* The main-thread detector only where a worker cannot run at all: on the main thread every
     inference stalls the face's own frames, so a worker that merely failed (a slow first load
     past the client's 10 s) is retried on the ladder instead. */
  function canWork() {
    return !!(window.Worker && window.OffscreenCanvas && window.createImageBitmap);
  }

  function load() {
    if (loading || now() < retryAt) return;
    loading = true;
    (canWork()
      ? import("/v3/face-worker-client.mjs").then(function (w) { return w.createFaceWorker(null, { flow: false }); })
      : import("/v3/agent-face-track.mjs").then(function (m) { return m.faceDetector(); }))
      .then(function (loaded) {
        loading = false;
        model = loaded;
        if (loaded.workerMode) loaded.onIdleError = function () { if (model === loaded) failed("idle"); };
      })
      .catch(function (err) { loading = false; failed("load: " + (err && err.message)); });
  }

  function ask(v, t) {
    lastAsk = t;
    if (model.workerMode) {
      var asked = model;
      model.detectAsync(v, t, function (result) {
        if (model === asked) { fails = 0; feed(result.boxes); }
      }, function (err) { if (model === asked) failed("frame: " + (err && err.message)); }, null);
      return;
    }
    var w = v.videoWidth, h = v.videoHeight;
    if (!(w > 0 && h > 0)) return;
    try {
      var result = model.detectForVideo(v, t);
      feed((result && result.detections || []).map(function (d) {
        var b = d.boundingBox;
        return { x: b.originX / w, y: b.originY / h, width: b.width / w, height: b.height / h };
      }));
    } catch (e) { failed("frame"); }
  }

  function tick() {
    var t = now(), v = camera();
    if (!v || trackerWatching(t)) {
      if (model && t - lastUse > CLOSE_AFTER_MS) close();
      return;
    }
    lastUse = t;
    if (!model) { load(); return; }
    var gap = window.LOW_POWER ? LOW_POWER_RATE_MS : RATE_MS;
    if (t - lastFace > SEARCH_AFTER_MS) gap = Math.max(gap, SEARCH_RATE_MS);
    if (t - lastAsk >= gap - 4 && !model.busy) ask(v, t);
  }

  window.gbEyes = {
    /* The leg tracker's boxes, every result it keeps, empty ones included: an empty one still
       means it is watching, so ours stays idle. */
    boxes: function (boxes) {
      lastFed = now();
      feed(boxes);
    },
    /* Whether this file would run its own detector right now. */
    wants: function () { return !!camera() && !trackerWatching(now()); },
    toScreen: toScreen,
    pick: pick
  };

  setInterval(tick, TICK_MS);
})();
