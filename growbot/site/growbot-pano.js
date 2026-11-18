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

   Exploring with it (owner, 2026-11-17, from the research every LLM explorer agrees on: the
   model picks WHERE, code does HOW): the strip is cut into 8 numbered ways, 45 degrees each.
   explore:{survey:true} turns him all the way round, in two half turns, so the strip fills;
   explore:{go:N} faces way N and walks him about a metre along it, a step per thought, until
   he has gone that far, two minutes pass, or he sends a move of his own or explore:{stop:true}.
   He still decides when to survey, which way to go, and when to stop.

   The pure half (column, marks, ways, travel steps) is tested in node; the page half reads the creature page's
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
  var GO_M = 0.9, GO_MS = 120000, FACE_DEG = 20, WALK_S = 25;

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

  // The 8 ways he can choose, numbered left to right along the strip, each between two marks.
  function ways(h0) {
    var out = [];
    for (var n = 1; n <= 8; n++) {
      var rel = 180 - 22.5 - 45 * (n - 1);
      out.push({ n: n, rel: rel, abs: wrap(h0 + rel), x: (180 - rel) * PX_DEG });
    }
    return out;
  }
  /* One step of a trip along a chosen way: turn to face it, walk while it is not walked yet,
     and end once he has gone GO_M from where it began, or after GO_MS. */
  function step(tr, h, now, walking) {
    var gone = Math.hypot(h.x - tr.x, h.y - tr.y);
    if (gone >= GO_M) return { done: "arrived", gone: gone };
    if (now - tr.t > GO_MS) return { done: "timed out", gone: gone };
    var d = wrap(tr.abs - h.h);
    if (Math.abs(d) > FACE_DEG) return { walk: { turn: Math.round(d) }, gone: gone };
    if (walking) return { gone: gone };
    return { walk: { gait: "screen_forward", dir: "fwd", secs: WALK_S }, gone: gone };
  }

  var api = { column: column, marks: marks, seenDeg: seenDeg, wrap: wrap, ways: ways, step: step, CAM_HFOV: CAM_HFOV, FW: FW, PX_DEG: PX_DEG };

  if (typeof window !== "undefined" && typeof document !== "undefined") {
    var shots = [], at = null, lastSent = { t: 0, seen: 0 }, shown = null, trip = null, survey = 0, lastTrip = null;
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

    // How much of the way round he has seen from this spot, for the explore line.
    api.seenHere = function () { try { return seenDeg(shots); } catch (e) { return 0; } };

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
        // The numbered ways, big enough to read, where he could choose to go.
        shown = ways(+h.h);
        g.font = "bold 22px sans-serif"; g.textBaseline = "middle";
        shown.forEach(function (w) {
          var cy = MARK_H + SH * 0.62;
          g.fillStyle = "rgba(0,0,0,0.6)"; g.beginPath(); g.arc(w.x, cy, 15, 0, 2 * Math.PI); g.fill();
          g.fillStyle = "#0ff"; var tw = g.measureText(String(w.n)).width; g.fillText(String(w.n), w.x - tw / 2, cy + 1);
        });
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
            + " another room (a different wall colour, a room beyond): that is where exploring goes next."
            + " The numbers 1 to 8 are the ways you can choose: explore:{go:N} turns you to way N and walks you about"
            + " a metre along it by itself.]"
        };
      } catch (e) { return null; }
    };

    function walkingNow() {
      try { var M = typeof _motion !== "undefined" ? _motion : null; var v = M && M.report && M.report.mv; return !!M && M.kind === "walk" && !(v && v.kind === "spin"); } catch (e) { return false; }
    }
    /* From the reply handler, before the map's own (which turns walk:{turn} into the spin that
       stops at the angle): his explore choice, and the trip's next step when he sends no move. */
    api.reply = function (o) {
      try {
        if (!o || typeof o !== "object" || (typeof MODE !== "undefined" && MODE !== "legs")) return;
        var h = here(), now = Date.now(), ex = o.explore;
        if (!h) return;
        var own = o.walk && typeof o.walk === "object";
        if (ex && typeof ex === "object") {
          if (ex.stop) { if (trip) lastTrip = { n: trip.n, why: "you stopped it" }; trip = null; survey = 0; }
          var n = Math.round(+ex.go);
          // Before any strip went out the ways are still where they would be drawn now.
          if (n >= 1 && n <= 8) {
            trip = { n: n, abs: (shown || ways(+h.h))[n - 1].abs, x: h.x, y: h.y, t: now };
            survey = 0; lastTrip = null; own = false; delete o.walk;
            try { logEv("explore_go", { n: n, abs: Math.round(trip.abs) }); } catch (e) {}
          } else if (ex.survey) { survey = 2; trip = null; own = false; delete o.walk; try { logEv("explore_survey", {}); } catch (e) {} }
        }
        // A move of his own outranks the trip and the survey: he chose it.
        if (own) { if (trip) lastTrip = { n: trip.n, why: "you moved yourself" }; trip = null; survey = 0; return; }
        if (trip) {
          var st = step(trip, h, now, walkingNow());
          if (st.done) { lastTrip = { n: trip.n, why: st.done, gone: st.gone }; try { logEv("explore_end", { n: trip.n, why: st.done, m: Math.round(st.gone * 100) / 100 }); } catch (e) {} trip = null; }
          else if (st.walk) o.walk = st.walk;
          return;
        }
        if (survey > 0 && !walkingNow()) { survey--; o.walk = { turn: 180 }; }
      } catch (e) {}
    };
    /* For the explore line: what the trip or survey is doing now. */
    api.state = function () {
      try {
        var h = here();
        return {
          trip: trip && h ? { n: trip.n, gone: Math.hypot(h.x - trip.x, h.y - trip.y) } : null,
          survey: survey, last: lastTrip, seen: seenDeg(shots), ways: !!shown
        };
      } catch (e) { return null; }
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.gbPano = api;
})(typeof window !== "undefined" ? window : this);
