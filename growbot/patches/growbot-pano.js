/* Dave's view all around (owner, 2026-11-17: "project his view onto a cylinder so that he
   probably understands where things are versus his angle"). One camera picture is 60 degrees
   of the room, and a turn's worth of them came one per thought, each forgotten by the next:
   in the map runs he looked the same way again and again and never back at the door behind
   him. Here every picture his camera takes while he stands in one spot is kept with the
   heading it faced (the gyro, gbMap's here.h), and painted onto a cylinder: one strip 360
   degrees wide with "ahead" in the middle, his left to the left and "behind" at both ends,
   each column at its true angle, and grey where he has not looked. After he turns, his next
   thought gets the strip beside his camera's picture, so a doorway is "about 120 degrees to
   my left", which walk:{turn:120} faces and map:{saw,deg} puts on his map.

   The pure half (column, marks) is tested in node; the page half reads the creature page's
   globals (camOn, camV, MODE) lazily. */
(function (root) {
  "use strict";
  var D = Math.PI / 180;
  // The sim's front camera, as growbot-map.js has it: 75 degrees tall, 360x480 portrait.
  var CAM_VFOV = 75, CAM_ASPECT = 360 / 480;
  var CAM_HFOV = 2 * Math.atan(Math.tan(CAM_VFOV * D / 2) * CAM_ASPECT) / D;
  var FW = 144, FH = 192, BIN = 3, PX_DEG = 3, MARK_H = 18;
  // Standing tall his camera looks about 30 degrees up, so the top of each picture is
  // ceiling (m7's first strips were half ceiling): the strip keeps the rest, taller.
  var CROP_TOP = 0.3;
  var MOVED_M = 0.3, KEEP_MS = 180000, NEW_DEG = 40, MIN_DEG = 120, GAP_MS = 8000;

  function wrap(a) { a = ((a + 180) % 360 + 360) % 360 - 180; return a === -180 ? 180 : a; }

  /* Which picture fills the column `rel` degrees from ahead (+ left), and where in it: the
     kept picture whose own heading is nearest that direction, if the direction falls inside
     its view, and the picture's column for it (0 its left edge). */
  function column(shots, h0, rel) {
    var az = h0 + rel, best = null, bd = 1e9, f = (FW / 2) / Math.tan(CAM_HFOV * D / 2);
    for (var i = 0; i < shots.length; i++) {
      var d = wrap(az - shots[i].h);
      if (Math.abs(d) < CAM_HFOV / 2 && Math.abs(d) < bd) { bd = Math.abs(d); best = shots[i]; }
    }
    if (!best) return null;
    var dd = wrap(az - best.h);
    return { shot: best, sx: Math.max(0, Math.min(FW - 1, Math.round(FW / 2 - f * Math.tan(dd * D)))) };
  }
  // The marks along the strip's top, every 45 degrees from behind-left to behind-right.
  function marks() {
    var out = [];
    for (var r = 180; r >= -180; r -= 45) {
      out.push({ rel: r, x: (180 - r) * PX_DEG, t: r === 0 ? "ahead" : Math.abs(r) === 180 ? "behind" : Math.abs(r) + (r > 0 ? "L" : "R") });
    }
    return out;
  }
  function seenDeg(shots) {
    var b = {}, n = 0;
    shots.forEach(function (s) {
      for (var d = -Math.floor(CAM_HFOV / 2); d < Math.floor(CAM_HFOV / 2); d++) {
        var k = ((Math.round(s.h) + d) % 360 + 360) % 360;
        if (!b[k]) { b[k] = 1; n++; }
      }
    });
    return n;
  }

  var api = { column: column, marks: marks, seenDeg: seenDeg, wrap: wrap, CAM_HFOV: CAM_HFOV, FW: FW, PX_DEG: PX_DEG };

  if (typeof window !== "undefined" && typeof document !== "undefined") {
    var shots = [], at = null, lastSent = { t: 0, seen: 0 };
    var here = function () {
      try { var m = window.gbMap && window.gbMap.map && window.gbMap.map(); return m && m.here; } catch (e) { return null; }
    };
    var video = function () {
      try { return typeof camOn !== "undefined" && camOn && typeof camV !== "undefined" && camV && camV.videoWidth ? camV : null; } catch (e) { return null; }
    };
    setInterval(function () {
      try {
        if (typeof MODE !== "undefined" && MODE !== "legs") return;
        var v = video(), h = here(), now = Date.now();
        if (!v || !h || !isFinite(+h.h)) return;
        // A strip is of one spot: walking on starts a new one.
        if (!at || Math.hypot(h.x - at.x, h.y - at.y) > MOVED_M) { shots = []; at = { x: h.x, y: h.y }; lastSent = { t: 0, seen: 0 }; }
        shots = shots.filter(function (s) { return now - s.t < KEEP_MS && Math.abs(wrap(s.h - h.h)) >= BIN; });
        var c = document.createElement("canvas");
        c.width = FW; c.height = FH;
        c.getContext("2d").drawImage(v, 0, 0, FW, FH);
        shots.push({ h: +h.h, c: c, t: now });
      } catch (e) {}
    }, 250);

    /* The strip for this thought, when he has turned enough since the last one to show it:
       most of a half turn seen here, and some of it new. */
    api.take = function () {
      try {
        if (typeof MODE !== "undefined" && MODE !== "legs") return null;
        var h = here(), now = Date.now();
        if (!h || !shots.length) return null;
        var seen = seenDeg(shots);
        if (seen < MIN_DEG || seen - lastSent.seen < NEW_DEG || now - lastSent.t < GAP_MS) return null;
        var W = 360 * PX_DEG, sy = Math.round(FH * CROP_TOP), SH = FH - sy, c = document.createElement("canvas"), g;
        c.width = W; c.height = SH + MARK_H; g = c.getContext("2d");
        g.fillStyle = "#808080"; g.fillRect(0, MARK_H, W, SH);
        g.fillStyle = "#000"; g.fillRect(0, 0, W, MARK_H);
        for (var x = 0; x < W; x++) {
          var col = column(shots, +h.h, 180 - x / PX_DEG);
          if (col) g.drawImage(col.shot.c, col.sx, sy, 1, SH, x, MARK_H, 1, SH);
        }
        g.font = "bold 14px sans-serif"; g.textBaseline = "top";
        marks().forEach(function (mk) {
          g.fillStyle = mk.rel === 0 ? "#ff0" : "#fff";
          g.fillRect(Math.min(W - 1, mk.x), 0, 1, MARK_H + SH);
          var tw = g.measureText(mk.t).width;
          g.fillText(mk.t, Math.max(1, Math.min(W - tw - 1, mk.x - tw / 2)), 2);
        });
        lastSent = { t: now, seen: seen };
        return {
          j: c.toDataURL("image/jpeg", 0.6).split(",")[1] || "",
          text: "[your view all around from where you stand, from the turns you just made: one strip 360 degrees wide."
            + " The middle (yellow line, ahead) is the way you face now, your left is to the left, behind you is at both ends;"
            + " the marks are degrees to your left (L) or right (R). Grey is where you have not looked from here (about "
            + Math.max(0, 360 - seen) + " degrees). To face something on it, turn by its mark (walk:{turn:N}, + to your left);"
            + " to put it on your map, map:{saw:\"name\",deg:N}. Look along it for a doorway or an opening into"
            + " another room (a different wall colour, a room beyond): that is where exploring goes next.]"
        };
      } catch (e) { return null; }
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.gbPano = api;
})(typeof window !== "undefined" ? window : this);
