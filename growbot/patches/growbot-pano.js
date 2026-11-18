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
  var SETTLE_MS = 1500, BLOCK_M = 0.15, CAM_VFOV = 75, CAM_ASPECT = 360 / 480;
  var CAM_HFOV = 2 * Math.atan(Math.tan(CAM_VFOV * D / 2) * CAM_ASPECT) / D;
  var FW = 144, FH = 192, BIN = 3, PX_DEG = 3, MARK_H = 18;
  // Standing tall his camera looks about 30 degrees up, so the top of each picture is
  // ceiling (m7's first strips were half ceiling): the strip keeps the rest, taller.
  var CROP_TOP = 0.3;
  var MOVED_M = 0.3, KEEP_MS = 180000, NEW_DEG = 40, MIN_DEG = 120, GAP_MS = 8000;
  var GO_M = 0.9, GO_MS = 120000, FACE_DEG = 20, WALK_S = 25;
  // Following the walls: short walks, a probe to the right after each clear one and a turn to
  // the left after each blocked one, for up to WALL_MS (mf9 ran out of ten minutes at the
  // living room's door, at his 4 cm a second).
  // His walk drifts right on its own (f5-f7), so the probe right is small.
  var BACK_S = 6, WALL_WALK_S = 12, WALL_MS = 1200000, PROBE_DEG = 15, WALL_BLOCKS = 16, SWING_DEG = 25;
  // His picture standing still while he walks forward (mean grey change per pixel, 2 s apart,
  // three in a row): in the flat (f3) 9 of 9 such runs were pushing against a wall or
  // furniture and none was walking, though it catches about a third of the pushing.
  var BLOCK_FLOW = 2.5, BLOCK_N = 3;

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
  function step(tr, h, now, walking, blocked) {
    var gone = Math.hypot(h.x - tr.x, h.y - tr.y);
    if (blocked) return { done: "something was in the way", gone: gone };
    if (gone >= GO_M) return { done: "arrived", gone: gone };
    if (now - tr.t > GO_MS) return { done: "timed out", gone: gone };
    var d = wrap(tr.abs - h.h);
    if (Math.abs(d) > FACE_DEG) { tr.from = null; return { walk: { turn: Math.round(d) }, gone: gone }; }
    if (walking) return { gone: gone };
    // A whole walk that got him nowhere: a wall or furniture is on that way.
    if (tr.from && Math.hypot(h.x - tr.from.x, h.y - tr.from.y) < BLOCK_M) return { done: "something was in the way", gone: gone };
    tr.from = { x: h.x, y: h.y };
    return { walk: { gait: "screen_forward", dir: "fwd", secs: WALK_S }, gone: gone };
  }

  /* One step of following the walls round (owner, 2026-11-17: "go around the perimeter of
     walls"), keeping them on his right, which takes him out through a doorway on the way: walk;
     a walk that something stopped turns him left, away from it; a clear one turns him a little
     right, back toward the wall, before the next. With no wall noticed every probe turns him
     further right, so he circles (f5, in a corner): noticing the walls is what makes it work. It ends when his map has him in another room,
     after WALL_MS, or when it keeps hitting things. blocked says the last walk was stopped. */
  function wallStep(tr, h, now, blocked, wedged) {
    var gone = Math.hypot(h.x - tr.x, h.y - tr.y);
    // Into another room by his map: two more walks carry him clear of the doorway (mf10 ended
    // its round in the doorway by his map's reckoning and spent ten minutes there).
    if (tr.room && h.room && h.room !== tr.room && tr.into === undefined) tr.into = tr.steps || 0;
    // Back in the room it began in (his map's drift by a doorway): not through after all.
    if (tr.into !== undefined && h.room === tr.room) tr.into = undefined;
    if (tr.into !== undefined && (tr.steps || 0) - tr.into >= 2) return { done: "you went through into the " + h.room, gone: gone };
    if (now - tr.t > WALL_MS) return { done: "it ran its twenty minutes", gone: gone };
    if (tr.blocks >= WALL_BLOCKS) return { done: "something kept being in the way", gone: gone };
    // The last turn went nowhere (gbMap's spin_stuck): back out first, then the same turn.
    if (wedged && !tr.walked) { tr.blocks++; tr.why = "wedged"; return { walk: { gait: "screen_backward", dir: "back", secs: BACK_S }, gone: gone }; }
    if (tr.walked) {
      tr.walked = false;
      // With the wall on his right, running into it swings him to the right as he walks: over
      // 61 walks in f5-f7, 11 of the 14 that swung 25 degrees or more right went under 0.3 m,
      // and a clear walk swings either way, left as much as 50 degrees.
      // side: 1 keeps the wall on his right, -1 on his left (mirror image).
      var side = tr.side === -1 ? -1 : 1, sw = typeof tr.h0 === "number" ? Math.round(wrap(h.h - tr.h0)) : 0;
      var swung = sw * side <= -SWING_DEG;
      tr.why = blocked ? "stopped" : swung ? "swung " + sw : "clear " + sw;
      if (swung) blocked = true;
      if (blocked) tr.blocks++;
      // A clear walk's probe is taken from where that walk set out, not where it ended: a walk
      // drifts up to 20 degrees either way (f8-f9, mf4-mf6), and turning on from the end let the
      // drift add up, so he ground into the wall or wandered off it.
      tr.want = blocked ? wrap(h.h + side * 90) : wrap((typeof tr.h0 === "number" ? tr.h0 : h.h) - side * PROBE_DEG);
      // A second wall within a couple of walks that would turn him back the way he came is a
      // corner taken on the wrong hand (f11: heading north into the corner by the door with the
      // wall on his right but following it on his left, he turned east into the side wall,
      // then south, home again): change hands and turn the other way from the first wall.
      if (blocked) {
        var lb = tr.lastBlock;
        if (lb && (tr.steps || 0) - lb.step <= 2 && Math.abs(wrap(tr.want - lb.h)) > 135) {
          tr.side = -side; tr.want = wrap(lb.h + tr.side * 90); tr.why += ", changed hands";
          tr.lastBlock = null;
        } else tr.lastBlock = { h: +h.h, step: tr.steps || 0 };
      }
    }
    if (tr.want !== null && tr.want !== undefined) {
      var d = wrap(tr.want - h.h);
      // Within the 8 degrees a gyro turn will not attempt (gbMap.turn) is facing it.
      if (Math.abs(d) > 8) return { walk: { turn: Math.round(d) }, gone: gone };
      tr.want = null;
    }
    tr.walked = true; tr.h0 = +h.h; tr.steps = (tr.steps || 0) + 1;
    return { walk: { gait: "screen_forward", dir: "fwd", secs: WALL_WALK_S }, gone: gone };
  }
  // Whether the last BLOCK_N picture changes (newest last) say he is pushing, not walking.
  function blockedRun(ds) {
    if (ds.length < BLOCK_N) return false;
    for (var i = ds.length - BLOCK_N; i < ds.length; i++) if (!(ds[i] < BLOCK_FLOW)) return false;
    return true;
  }

  var api = { column: column, marks: marks, seenDeg: seenDeg, wrap: wrap, ways: ways, step: step, wallStep: wallStep, blockedRun: blockedRun, CAM_HFOV: CAM_HFOV, FW: FW, PX_DEG: PX_DEG };

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

    /* How much his picture changes while he walks forward (a small grey thumbnail, 2 s
       apart): the phone cannot tell walking from pushing against a wall, and his map's dead
       reckoning moves him either way. Logged as "flow" to learn the threshold. */
    var thumbs = [];
    function thumb(v) {
      var c = document.createElement("canvas"); c.width = 16; c.height = 20;
      var g = c.getContext("2d"); g.drawImage(v, 0, 0, 16, 20);
      var d = g.getImageData(0, 0, 16, 20).data, out = new Array(320);
      for (var i = 0; i < 320; i++) out[i] = (d[i * 4] + d[i * 4 + 1] + d[i * 4 + 2]) / 3;
      return out;
    }
    var hist = [];
    function diff(a, b) { var sum = 0; for (var i = 0; i < 320; i++) sum += Math.abs(a[i] - b[i]); return sum / 320; }
    // The share of the thumbnail within 6 grey levels of its median, in hundredths.
    function flat(p) {
      var q = p.slice().sort(function (x, y) { return x - y; }), md = q[160], n = 0;
      for (var i = 0; i < 320; i++) if (Math.abs(p[i] - md) <= 6) n++;
      return Math.round(n / 3.2);
    }
    function fwdWalking() {
      try { var M = typeof _motion !== "undefined" ? _motion : null, v = M && M.report && M.report.mv; return !!M && !M.loading && M.kind === "walk" && M.own !== "back" && !(v && (v.kind === "spin" || v.direction === "turn" || v.direction === "screen-backward")); } catch (e) { return false; }
    }
    setInterval(function () {
      try {
        if (typeof MODE !== "undefined" && MODE !== "legs") return;
        var v = video(), h = here(), now = Date.now();
        if (!v || !h || !fwdWalking()) { thumbs = []; hist = []; flows = []; return; }
        var p = thumb(v);
        thumbs.push({ t: now, p: p, h: +h.h });
        hist.push(p); hist = hist.slice(-5);
        if (thumbs.length < 3) return;
        var a = thumbs[0].p, b = thumbs[thumbs.length - 1].p;
        var dd = Math.round(diff(a, b) * 10) / 10;
        // Also logged, to find a better sign than dd (which caught a third of the walls): the
        // change over four seconds, which a shake does not build up, and how much of the
        // picture is one flat shade, as a wall right in front of him is.
        var d4 = hist.length === 5 ? Math.round(diff(hist[0], p) * 10) / 10 : null;
        try { logEv("flow", { d: dd, d4: d4, u: flat(p), turn: Math.round(wrap(h.h - thumbs[0].h)) }); } catch (e) {}
        thumbs = thumbs.slice(-1);
        flows.push(dd); flows = flows.slice(-BLOCK_N);
        if (blockedRun(flows)) onBlocked();
      } catch (e) {}
    }, 1000);
    var flows = [], blockedT = 0, blockedStep = false;
    /* Pushing against something: the walk stops, since the phone cannot feel the wall and his
       map would carry him on through it. On a trip the step is marked blocked; on a walk of his
       own while exploring, his next thought is told. */
    function onBlocked() {
      flows = [];
      var mine = !!(trip || survey > 0), exploring = false;
      try { exploring = typeof GB_EXPLORE_RE !== "undefined" && GB_EXPLORE_RE.test(typeof seekGoal !== "undefined" && seekGoal || ""); } catch (e) {}
      try { logEv("blocked", { trip: trip ? trip.n : 0, own: !mine }); } catch (e) {}
      if (!mine && !exploring) return;
      blockedT = Date.now();
      if (mine) blockedStep = true;
      try {
        var M = _motion; if (M && M.report) M.report._gbTurned = 1;
        var g0 = _motionGen; runGait({ dir: "stop" }); for (var g = g0 + 1; g <= _motionGen; g++) _gbDoneGen[g] = 1;
      } catch (e) {}
    }
    api.blockedAgo = function () { return blockedT ? Date.now() - blockedT : null; };
    api.wedgedAgo = function () { var t = window.gbMap && window.gbMap.stuckSpinT ? window.gbMap.stuckSpinT() : 0; return t ? Date.now() - t : null; };

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

    /* From the reply handler, before the map's own (which turns walk:{turn} into the spin that
       stops at the angle): his explore choice, and the trip's next step when he sends no move. */
    // Whether a thought issued at generation g was overtaken, by the page's own test for
    // refusing its move (motion_refused).
    function overtaken(g) {
      try { return typeof _motionGen === "number" && _motionGen !== g && !(typeof _gbOnlyDone === "function" && _gbOnlyDone(g, _motionGen)); } catch (e) { return false; }
    }
    function stoppedSince(t) { try { return (window._gbFollowStopT || 0) >= t; } catch (e) { return false; } }
    var wallSide = -1;
    api.reply = function (o, gen, issT) {
      try {
        if (!o || typeof o !== "object" || (typeof MODE !== "undefined" && MODE !== "legs")) return;
        var h = here(), now = Date.now(), ex = o.explore, chose = false;
        if (!h) return;
        var own = o.walk && typeof o.walk === "object";
        if (ex && typeof ex === "object") {
          if (ex.stop) { if (trip) lastTrip = { n: trip.n, wall: !!trip.wall, why: "you stopped it" }; trip = null; survey = 0; }
          var n = Math.round(+ex.go);
          // A thought overtaken by a stop, or landing while he is paused or out of sight, starts
          // nothing: the page refuses its move too (motion_refused).
          if ((n >= 1 && n <= 8 || ex.survey || ex.walls) && ((typeof gen === "number" && overtaken(gen)) || (issT && stoppedSince(issT)) || (typeof paused !== "undefined" && paused) || document.hidden)) {
            try { logEv("explore_refused", {}); } catch (e) {}
            n = 0; ex = {};
            delete o.walk; own = false;
          }
          // Before any strip went out the ways are still where they would be drawn now.
          if (n >= 1 && n <= 8) {
            trip = { n: n, abs: (shown || ways(+h.h))[n - 1].abs, x: h.x, y: h.y, t: now };
            survey = 0; lastTrip = null; own = false; delete o.walk; chose = true;
            try { logEv("explore_go", { n: n, abs: Math.round(trip.abs) }); } catch (e) {}
          } else if (ex.walls && trip && trip.wall) {
            // Asked again while it runs: it carries on, not starts over.
            own = false; delete o.walk; chose = true;
          } else if (ex.walls) {
            /* Which hand: a round keeping the wall on one side can circle a part of the room
               that never reaches its door (mf4: round the couch's far end, a metre short of
               the door, then back), so each new round takes the other side unless he names
               one (explore:{walls:"left"}). */
            var sd = ex.walls === "left" ? -1 : ex.walls === "right" ? 1 : (wallSide = -wallSide);
            trip = { n: 0, wall: true, side: sd, x: h.x, y: h.y, t: now, room: h.room || "", want: null, walked: false, blocks: 0 };
            survey = 0; lastTrip = null; own = false; delete o.walk; chose = true;
            try { logEv("explore_walls", { room: h.room || "", side: sd }); } catch (e) {}
          } else if (ex.survey) { survey = 2; trip = null; own = false; delete o.walk; chose = true; try { logEv("explore_survey", {}); } catch (e) {} }
          // A gesture beside the choice would only be cut short by the trip's first step.
          if (chose && !(trip && trip.wall && trip.t < now)) { startT = now; tripRep = null; blockedStep = false; }
          if (chose) { delete o.gesture; delete o.body; delete o.parts; }
          // The move this reply chose is the trip's: no "you said you were moving" nudge.
          if (chose) o._gbExplored = 1;
        }
        /* Following the walls is a round of a few minutes that his own turns kept cutting
           short (f4: six rounds, none longer than two minutes, each ended by a spin to "find
           the way"). While it runs, a move of his own is left out; a stop, or
           explore:{stop:true}, still ends it. */
        if (own && trip && trip.wall && !/^(stop|halt)$/i.test(String(o.walk.dir || ""))) {
          delete o.walk; own = false;
          try { logEv("explore_kept", {}); } catch (e) {}
        }
        // A move of his own outranks the trip and the survey: he chose it.
        if (own) { if (trip) lastTrip = { n: trip.n, wall: !!trip.wall, why: "you moved yourself" }; trip = null; survey = 0; }
      } catch (e) {}
    };
    /* The trip and the survey move him between his thoughts, not one step per thought: a
       thought takes 20 s or more, and the next one, seeing a turn half done, sent a move of
       its own and ended the trip it had just chosen (live m11, m12). Each step starts only
       once the last move has ended and his legs have had a moment to settle. */
    var idleT = 0, startT = 0, tripRep = null;
    setInterval(function () {
      try {
        if ((!trip && survey <= 0) || (typeof MODE !== "undefined" && MODE !== "legs")) { idleT = 0; return; }
        // A stop ends the trip and the survey, so they never set him off again after one: "stop
        // stop" stamps _gbFollowStopT, and a step that ended cancelled, blocked or failed was
        // stopped (a pause, the stop button, a grab), unless it was the gyro ending a turn as
        // asked. Generations are not the test: a gesture, a drive or a spin that runs out its
        // time each leave one not marked done in normal running.
        var cut = stoppedSince(startT) ? "stop" : "";
        // The stop button is the pause: pressed between steps, with no step to cancel, it ends
        // the trip too. The Brain panel's own pause (not hard) only holds it.
        if (!cut && typeof paused !== "undefined" && paused && typeof _pauseHard !== "undefined" && _pauseHard) cut = "paused";
        if (!cut && tripRep && !(typeof _motion !== "undefined" && _motion) && /^(cancelled|blocked|failed)$/.test(tripRep.state || "") && !tripRep._gbTurned) cut = tripRep.reason || tripRep.state;
        if (cut) {
          if (trip) lastTrip = { n: trip.n, wall: !!trip.wall, why: "you were stopped" };
          try { logEv("explore_end", { n: trip ? trip.n : 0, why: "stopped", by: String(cut).slice(0, 40) }); } catch (e) {}
          trip = null; survey = 0; idleT = 0; tripRep = null; return;
        }
        if ((typeof _motion !== "undefined" && _motion) || (typeof dreaming !== "undefined" && dreaming) || (typeof paused !== "undefined" && paused) || document.hidden || typeof runGait !== "function") { idleT = 0; return; }
        var now = Date.now(), h = here();
        if (!h) return;
        if (!idleT) idleT = now;
        if (now - idleT < SETTLE_MS) return;
        idleT = 0;
        var spec = null;
        if (trip) {
          var bl = blockedStep; blockedStep = false;
          // A room renamed while the round runs ("room 1" named the living room) is the same room.
          try { var mm = window.gbMap.map(); if (trip.wall && trip.room && !(mm.rooms || []).some(function (r) { return r.n === trip.room; })) trip.room = h.room || ""; } catch (e) {}
          var sst = window.gbMap && window.gbMap.stuckSpinT ? window.gbMap.stuckSpinT() : 0, wedged = sst > (trip.stuckT || trip.t);
          if (wedged) trip.stuckT = sst;
          var st = trip.wall ? wallStep(trip, h, now, bl, wedged) : step(trip, h, now, false, bl);
          if (st.done) { lastTrip = { n: trip.n, wall: !!trip.wall, why: st.done, gone: st.gone }; try { logEv("explore_end", { n: trip.n, why: st.done, m: Math.round(st.gone * 100) / 100 }); } catch (e) {} trip = null; wake(); return; }
          spec = st.walk || null;
        } else { survey--; spec = { turn: 180 }; }
        if (spec && spec.turn !== undefined) spec = window.gbMap && window.gbMap.turn ? window.gbMap.turn(spec.turn) : null;
        if (!spec) return;
        try { logEv("explore_step", { trip: trip ? trip.n : 0, gait: spec.gait, secs: spec.secs, h: Math.round(+h.h), why: trip && trip.why || "" }); } catch (e) {}
        if (trip) trip.why = "";
        // The trip's moves are his choice carried out, not news that overtakes a thought in
        // flight: their generations are marked done, or that thought's move is refused.
        var g0 = _motionGen;
        runGait(spec);
        for (var g = g0 + 1; g <= _motionGen; g++) _gbDoneGen[g] = 1;
        tripRep = _motion && _motion.report || null;
        // A move the page refused (duty limit, cooling legs, no body) is not a wall on that way.
        if (!_motion) {
          if (trip) { lastTrip = { n: trip.n, wall: !!trip.wall, why: "your legs could not move just then" }; try { logEv("explore_end", { n: trip.n, why: "refused" }); } catch (e) {} trip = null; wake(); }
          survey = 0;
        }
      } catch (e) {}
    }, 500);
    /* A trip that ends by itself is his news: waiting on an event or his person while it ran
       (mf11), he heard nothing for four minutes after it ended. One turn, through the page's
       own timer wake. */
    function wake() {
      try { if (typeof actPhase !== "undefined" && actPhase === "waiting" && (actWake !== "timer" || actNotBefore > Date.now())) { actWake = "timer"; actNotBefore = Date.now(); logEv("explore_wake", {}); } } catch (e) {}
    }
    /* For the explore line: what the trip or survey is doing now. */
    api.state = function () {
      try {
        var h = here();
        return {
          trip: trip && h ? { n: trip.n, wall: !!trip.wall, side: trip.side === -1 ? "left" : "right", gone: Math.hypot(h.x - trip.x, h.y - trip.y) } : null,
          survey: survey, last: lastTrip, seen: seenDeg(shots), ways: !!shown
        };
      } catch (e) { return null; }
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.gbPano = api;
})(typeof window !== "undefined" ? window : this);
