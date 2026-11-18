/* Dave's sketch map of the house (owner, 2026-11-17: "draw geometric shapes to approximate the
   geometry it has seen ... defaulting the room to square and putting in doorways, and then
   extrapolating it out", with tools "not only to log it but also query it", and to change it
   "if he thinks it's changed or has changed his mind").

   A rough sketch, not a survey: the phone knows which way it faces (the gyro) but not where it
   is, so his position is dead reckoning from his own walks, and it drifts. Rooms are rectangles
   (a new one is a 4 m square), doorways sit on their walls, things are points. What he sees
   reshapes it: a doorway or wall seen at a distance moves that wall there, a doorway into a room
   he has not been in sketches that room beyond it, and reaching a thing he knows ("at") puts him
   back where the map says it is, which is what keeps the drift bounded.

   It lives in the soul (`soul.map`), so the box keeps it with his memory and the PWA draws it.
   Coordinates are metres; heading is degrees counter-clockwise from +x, the way his camera
   looks. A new map starts with him at the origin facing 90 (up on the drawing).

   The pure half (gbMap.apply / where / live) takes the map as an argument and is tested in node;
   the page half (the pose tracker, the reply hooks) reads the creature page's globals lazily. */
(function (root) {
  "use strict";
  var D = Math.PI / 180;
  var ROOM_M = 4, ROOM_MIN = 1.2, ROOM_MAX = 15, ROOMS_MAX = 24, THINGS_MAX = 120, DOORS_MAX = 48;
  var NAME_MAX = 32, OPS_MAX = 4, SEEN_M = 1.5, MOVED_M = 1.5, AT_M = 0.35, INSET_M = 0.3;
  // The phone's front camera as the sim has it (360x480 portrait, 75 degrees tall): about 60
  // degrees across. A real phone's is close; a bearing is a rough aim, and he looks again.
  var CAM_VFOV = 75, CAM_ASPECT = 360 / 480;
  var CAM_HFOV = 2 * Math.atan(Math.tan(CAM_VFOV * D / 2) * CAM_ASPECT) / D;
  var DIRS = {
    ahead: 0, front: 0, forward: 0, straight: 0, "ahead-left": 45, "front-left": 45,
    left: 90, "behind-left": 135, "back-left": 135, behind: 180, back: 180,
    "behind-right": -135, "back-right": -135, right: -90, "ahead-right": -45, "front-right": -45,
  };

  function wrap(a) { a = ((a + 180) % 360 + 360) % 360 - 180; return a === -180 ? 180 : a; }
  function r2(v) { return Math.round(v * 100) / 100; }
  function num(v, lo, hi) { v = typeof v === "string" ? parseFloat(v) : v; return typeof v === "number" && isFinite(v) && v >= lo && v <= hi ? v : null; }
  function name(v) {
    // A name is read back into his prompt, so it is a plain phrase: no quotes, braces or markup.
    var s = typeof v === "string" ? v.replace(/[{}"<>|\\=`\[\]]+/g, " ").replace(/\s+/g, " ").trim().toLowerCase() : "";
    s = s.replace(/^(the|a|an|my|your)\s+/, "").slice(0, NAME_MAX);
    return !s || /^(omit|none|null|nothing|n\/?a|unknown|-|\?)$/.test(s) ? "" : s;
  }

  function fresh(now) { return { v: 1, t: now || 0, rooms: [], doors: [], things: [], here: { x: 0, y: 0, h: 90, room: "", t: now || 0 } }; }
  // A stored map is the phone's own JSON, so every field is checked before it is trusted.
  function norm(m, now) {
    if (!m || typeof m !== "object" || m.v !== 1) return fresh(now);
    var out = fresh(+m.t || 0), h = m.here || {};
    out.here = { x: num(h.x, -1e4, 1e4) || 0, y: num(h.y, -1e4, 1e4) || 0, h: num(h.h, -1e4, 1e4) === null ? 90 : wrap(h.h), room: name(h.room), t: +h.t || 0 };
    (Array.isArray(m.rooms) ? m.rooms : []).slice(0, ROOMS_MAX).forEach(function (r) {
      var n = name(r && r.n), x = num(r && r.x, -1e4, 1e4), y = num(r && r.y, -1e4, 1e4), w = num(r && r.w, ROOM_MIN, ROOM_MAX), d = num(r && r.d, ROOM_MIN, ROOM_MAX);
      if (n && x !== null && y !== null && w && d && !roomOf(out, n)) out.rooms.push({ n: n, x: x, y: y, w: w, d: d, t: +r.t || 0 });
    });
    (Array.isArray(m.doors) ? m.doors : []).slice(0, DOORS_MAX).forEach(function (o) {
      var x = num(o && o.x, -1e4, 1e4), y = num(o && o.y, -1e4, 1e4);
      if (x !== null && y !== null) out.doors.push({ x: x, y: y, a: name(o.a), b: name(o.b), t: +o.t || 0 });
    });
    (Array.isArray(m.things) ? m.things : []).slice(0, THINGS_MAX).forEach(function (o) {
      var n = name(o && o.n), x = num(o && o.x, -1e4, 1e4), y = num(o && o.y, -1e4, 1e4);
      if (n && x !== null && y !== null && !thingOf(out, n)) out.things.push({ n: n, x: x, y: y, room: name(o.room), k: Math.min(5, Math.max(1, +o.k || 1)), t: +o.t || 0, rough: o.rough ? 1 : undefined });
    });
    return out;
  }

  function roomOf(m, n) { for (var i = 0; i < m.rooms.length; i++) if (m.rooms[i].n === n) return m.rooms[i]; return null; }
  function thingOf(m, n) { for (var i = 0; i < m.things.length; i++) if (m.things[i].n === n) return m.things[i]; return null; }
  function inside(r, x, y, pad) { pad = pad || 0; return Math.abs(x - r.x) <= r.w / 2 + pad && Math.abs(y - r.y) <= r.d / 2 + pad; }
  function roomAt(m, x, y, prefer) {
    var p = prefer && roomOf(m, prefer);
    if (p && inside(p, x, y)) return p.n;
    for (var i = 0; i < m.rooms.length; i++) if (inside(m.rooms[i], x, y)) return m.rooms[i].n;
    return "";
  }
  function overlap(a, b) {
    return Math.max(0, Math.min(a.x + a.w / 2, b.x + b.w / 2) - Math.max(a.x - a.w / 2, b.x - b.w / 2)) *
      Math.max(0, Math.min(a.y + a.d / 2, b.y + b.d / 2) - Math.max(a.y - a.d / 2, b.y - b.d / 2));
  }
  function clampInto(r, x, y) {
    var ix = Math.max(0.05, r.w / 2 - INSET_M), iy = Math.max(0.05, r.d / 2 - INSET_M);
    return { x: Math.min(r.x + ix, Math.max(r.x - ix, x)), y: Math.min(r.y + iy, Math.max(r.y - iy, y)) };
  }

  /* Where something he sees is: a bearing from where it sits across his picture (x: 0 the left
     edge, 1 the right), else from a direction word, and a distance from his guess in metres,
     else from how much of the picture's height it fills and how tall such a thing is. */
  function bearingOf(op) {
    var x = num(op.x, 0, 1);
    if (x !== null) return Math.atan((0.5 - x) * 2 * Math.tan(CAM_HFOV * D / 2)) / D;
    var w = typeof op.dir === "string" ? op.dir.trim().toLowerCase().replace(/\s+/g, "-") : "";
    if (Object.prototype.hasOwnProperty.call(DIRS, w)) return DIRS[w];
    var deg = num(op.deg, -180, 180);
    return deg === null ? 0 : deg;
  }
  function distOf(op, dflt) {
    var m = num(op.m, 0.1, 30);
    if (m !== null) return m;
    var h = num(op.h, 0.02, 1), size = num(op.size, 0.05, 5);
    if (h !== null && size !== null) return Math.min(30, size / (h * 2 * Math.tan(CAM_VFOV * D / 2)));
    return dflt;
  }
  function seen(m, op, dflt) {
    var b = bearingOf(op), d = distOf(op, dflt), a = (m.here.h + b) * D;
    return { x: m.here.x + d * Math.cos(a), y: m.here.y + d * Math.sin(a), b: b, d: d };
  }
  function relTo(m, x, y) {
    var dx = x - m.here.x, dy = y - m.here.y;
    return { d: Math.hypot(dx, dy), b: wrap(Math.atan2(dy, dx) / D - m.here.h) };
  }
  function side(b) {
    var a = Math.abs(b);
    if (a < 12) return "straight ahead";
    if (a > 165) return "behind you";
    return Math.round(a) + " degrees to your " + (b > 0 ? "left" : "right");
  }
  function far(d) { return d < 0.5 ? "right beside you" : "about " + (d < 3 ? r2(Math.round(d * 10) / 10) : Math.round(d)) + " m away"; }

  /* A new room is a square (or the size he said) around where he is. Where that would cover a
     room already drawn, it is pushed out to sit against that room's nearest wall instead, the
     way a room through a doorway sits beside the one he came from. */
  function placeRoom(m, n, w, d, cx, cy) {
    var r = { n: n, x: cx, y: cy, w: w, d: d };
    for (var pass = 0; pass < 3; pass++) {
      var hit = null;
      for (var i = 0; i < m.rooms.length; i++) if (overlap(r, m.rooms[i]) > 0.05) { hit = m.rooms[i]; break; }
      if (!hit) break;
      var opts = [
        { x: hit.x + hit.w / 2 + w / 2, y: r.y }, { x: hit.x - hit.w / 2 - w / 2, y: r.y },
        { x: r.x, y: hit.y + hit.d / 2 + d / 2 }, { x: r.x, y: hit.y - hit.d / 2 - d / 2 },
      ];
      opts.sort(function (p, q) { return Math.hypot(p.x - cx, p.y - cy) - Math.hypot(q.x - cx, q.y - cy); });
      r.x = opts[0].x; r.y = opts[0].y;
    }
    return r;
  }
  /* The wall of room r that a ray from inside at angle a (degrees) meets first: which side, and
     where. Rooms are axis-aligned, so it is the nearest of the four crossings ahead. */
  function wallHit(r, x, y, a) {
    var c = Math.cos(a * D), s = Math.sin(a * D), best = null;
    function t(k, v) { if (v > 1e-6 && (!best || v < best.t)) best = { k: k, t: v }; }
    if (c > 1e-6) t("e", (r.x + r.w / 2 - x) / c);
    if (c < -1e-6) t("w", (r.x - r.w / 2 - x) / c);
    if (s > 1e-6) t("n", (r.y + r.d / 2 - y) / s);
    if (s < -1e-6) t("s", (r.y - r.d / 2 - y) / s);
    if (!best) return null;
    return { k: best.k, x: x + best.t * c, y: y + best.t * s, t: best.t };
  }
  // Moves one wall of r to pass through (px, py), keeping the opposite wall where it is.
  function moveWall(r, k, px, py) {
    var lo, hi;
    if (k === "e" || k === "w") {
      lo = k === "e" ? r.x - r.w / 2 : px; hi = k === "e" ? px : r.x + r.w / 2;
      if (hi - lo < ROOM_MIN || hi - lo > ROOM_MAX) return false;
      r.x = (lo + hi) / 2; r.w = hi - lo;
    } else {
      lo = k === "n" ? r.y - r.d / 2 : py; hi = k === "n" ? py : r.y + r.d / 2;
      if (hi - lo < ROOM_MIN || hi - lo > ROOM_MAX) return false;
      r.y = (lo + hi) / 2; r.d = hi - lo;
    }
    return true;
  }
  // The side of a wall k faces: the direction out of the room through it.
  var OUT = { e: [1, 0], w: [-1, 0], n: [0, 1], s: [0, -1] };

  function here(m) { return m.here.room && roomOf(m, m.here.room); }
  function ensureRoom(m, now) {
    var r = here(m);
    if (r) return r;
    // He has not named a room yet: sketch one around him, to be named when he does.
    var n = roomAt(m, m.here.x, m.here.y);
    if (n) { m.here.room = n; return roomOf(m, n); }
    if (m.rooms.length >= ROOMS_MAX) return null;
    r = placeRoom(m, "room " + (m.rooms.length + 1), ROOM_M, ROOM_M, m.here.x, m.here.y);
    r.t = now; m.rooms.push(r); m.here.room = r.n;
    return r;
  }

  /* "room", "room1", "room a" or a bare letter tells his map nothing it can be asked for later
     ("where is the kitchen?"), and "room N" squats the name the map gives an unnamed room
     itself; mf9 and mf10 called them "start", "outside_start" and "unknown room", names for
     where he began or what he did not know, not the room. */
  function notName(n) {
    if (!/^(the )?((room|area|space|place)( ?([a-z]|\d+))?|[a-z]|\d+)$/.test(n) && !/\b(start|outside|unknown|unnamed)/.test(n)) return "";
    return "\"" + n + "\" is not a name: call a room what it is by what is in it (a piano or couch makes a living room, a fridge or stove a kitchen, a bed a bedroom, a long narrow way with doorways off it a hallway)";
  }
  var OPS = {
    room: function (m, op, now) {
      var n = name(op.room), size = Array.isArray(op.size) ? op.size : [];
      if (!n) return "";
      if (notName(n)) return notName(n);
      var w = num(size[0], ROOM_MIN, ROOM_MAX), d = num(size[1], ROOM_MIN, ROOM_MAX), r = roomOf(m, n), cur = here(m);
      if (!r && cur && /^room \d+$/.test(cur.n) && inside(cur, m.here.x, m.here.y)) {
        // The room sketched before he named it is this one: its doorways and things too.
        var old = cur.n;
        cur.n = n; r = cur;
        m.doors.forEach(function (o) { if (o.a === old) o.a = n; if (o.b === old) o.b = n; });
        m.things.forEach(function (t) { if (t.room === old) t.room = n; });
      }
      if (r) {
        if (w) { r.w = w; } if (d) { r.d = d; }
        r.t = now; m.here.room = n;
        if (!inside(r, m.here.x, m.here.y)) {
          var p = clampInto(r, m.here.x, m.here.y), moved = Math.hypot(p.x - m.here.x, p.y - m.here.y);
          m.here.x = p.x; m.here.y = p.y;
          return "you are in the " + n + " (your place moved " + r2(moved) + " m to be inside it)";
        }
        return "you are in the " + n;
      }
      if (m.rooms.length >= ROOMS_MAX) return "";
      r = placeRoom(m, n, w || ROOM_M, d || ROOM_M, m.here.x, m.here.y);
      r.t = now; m.rooms.push(r);
      if (!inside(r, m.here.x, m.here.y)) { var q = clampInto(r, m.here.x, m.here.y); m.here.x = q.x; m.here.y = q.y; }
      m.here.room = n;
      return "a new room on your map: the " + n + " (sketched as " + r2(r.w) + " by " + r2(r.d) + " m)";
    },
    saw: function (m, op, now, force) {
      var n = name(op.saw || op.fix || op.moved);
      if (!n) return "";
      var t = thingOf(m, n), rn = name(op.room), hasPos = op.x !== undefined || op.dir !== undefined || op.deg !== undefined || op.m !== undefined;
      ensureRoom(m, now);
      var p, rough;
      if (force && rn && !hasPos) {
        // He knows the room, not the spot: the middle of it, marked rough.
        var r = roomOf(m, rn);
        if (!r) return "there is no " + rn + " on your map yet";
        p = { x: r.x, y: r.y }; rough = 1;
      } else p = seen(m, op, SEEN_M);
      var room = rn && roomOf(m, rn) ? rn : roomAt(m, p.x, p.y, m.here.room) || m.here.room;
      if (!t) {
        if (m.things.length >= THINGS_MAX) return "";
        m.things.push({ n: n, x: p.x, y: p.y, room: room, k: 1, t: now, rough: rough });
        return "the " + n + " is on your map" + (room ? " in the " + room : "");
      }
      var gap = Math.hypot(p.x - t.x, p.y - t.y);
      if (force || rough || gap > MOVED_M) {
        t.x = p.x; t.y = p.y; t.room = room; t.k = 1; t.t = now; t.rough = rough;
        return "the " + n + " moved on your map" + (room ? ", now in the " + room : "") + " (" + r2(gap) + " m from where you had it)";
      }
      // Seen again close to where it was: each sighting refines it, the newest weighted most.
      var k = Math.min(t.k, 4);
      t.x = (t.x * k + p.x) / (k + 1); t.y = (t.y * k + p.y) / (k + 1);
      t.k = Math.min(5, t.k + 1); t.t = now; t.rough = undefined;
      if (!t.room) t.room = room;
      return "";
    },
    door: function (m, op, now) {
      // A doorway to a room he cannot name yet is still a doorway: its far side stays unnamed.
      var to = name(op.door), r = ensureRoom(m, now);
      if (to && notName(to)) to = "";
      if (!r) return "";
      var b = bearingOf(op), a = m.here.h + b, w = wallHit(r, m.here.x, m.here.y, a);
      if (!w) return "";
      var px = w.x, py = w.y, moved = "";
      var dm = distOf(op, null);
      if (dm !== null) {
        // He saw how far it is: that wall is there, so the room grows or shrinks to meet it.
        px = m.here.x + dm * Math.cos(a * D); py = m.here.y + dm * Math.sin(a * D);
        var before = { x: r.x, y: r.y, w: r.w, d: r.d };
        if (moveWall(r, w.k, px, py)) {
          if (Math.abs(before.w - r.w) + Math.abs(before.d - r.d) > 0.2) moved = ", and the " + r.n + " is now " + r2(r.w) + " by " + r2(r.d) + " m";
          // The doorway sits on the wall, wherever along it he saw it.
          if (w.k === "e" || w.k === "w") px = r.x + (w.k === "e" ? r.w / 2 : -r.w / 2); else py = r.y + (w.k === "n" ? r.d / 2 : -r.d / 2);
        } else { px = w.x; py = w.y; }
      }
      var o = null;
      for (var i = 0; i < m.doors.length; i++) {
        var e = m.doors[i];
        if (Math.hypot(e.x - px, e.y - py) < 1 && (e.a === r.n || e.b === r.n)) { o = e; break; }
      }
      if (!o) {
        if (m.doors.length >= DOORS_MAX) return "";
        o = { x: px, y: py, a: r.n, b: "", t: now }; m.doors.push(o);
      } else { o.x = (o.x + px) / 2; o.y = (o.y + py) / 2; o.t = now; }
      if (to && to !== r.n) {
        var other = o.a === r.n ? "b" : "a";
        o[other] = to;
        if (!roomOf(m, to) && m.rooms.length < ROOMS_MAX) {
          // A room he has not been in yet: sketched as a square beyond the doorway.
          var u = OUT[w.k], nr = placeRoom(m, to, ROOM_M, ROOM_M, o.x + u[0] * ROOM_M / 2, o.y + u[1] * ROOM_M / 2);
          nr.t = now; m.rooms.push(nr);
          return "a doorway to the " + to + " is on your map, " + side(b) + moved + "; the " + to + " is sketched beyond it";
        }
      }
      return "a doorway" + (to && to !== r.n ? " to the " + to : "") + " is on your map, " + side(b) + moved;
    },
    wall: function (m, op, now) {
      var r = ensureRoom(m, now), dm = distOf(op, null);
      if (!r || dm === null) return "";
      var b = bearingOf(op), a = m.here.h + b, w = wallHit(r, m.here.x, m.here.y, a);
      if (!w) return "";
      if (!moveWall(r, w.k, m.here.x + dm * Math.cos(a * D), m.here.y + dm * Math.sin(a * D))) return "";
      r.t = now;
      return "the " + r.n + " is now " + r2(r.w) + " by " + r2(r.d) + " m on your map";
    },
    at: function (m, op, now) {
      var n = name(op.at), t = thingOf(m, n);
      if (!n) return "";
      if (!t) {
        // Somewhere new beside him: it goes on the map where he is.
        OPS.saw(m, { saw: n, m: AT_M }, now);
        return "the " + n + " is on your map, beside you";
      }
      // Beside it, on the side he came from: this is what corrects his drift.
      var dx = m.here.x - t.x, dy = m.here.y - t.y, d = Math.hypot(dx, dy) || 1;
      var nx = t.x + dx / d * AT_M, ny = t.y + dy / d * AT_M, fix = Math.hypot(nx - m.here.x, ny - m.here.y);
      m.here.x = nx; m.here.y = ny; m.here.t = now;
      if (t.room) m.here.room = t.room;
      t.t = now;
      return "you are at the " + n + (fix > 0.3 ? " (your place on the map moved " + r2(fix) + " m)" : "");
    },
    forget: function (m, op) {
      var n = name(op.forget), i;
      for (i = 0; i < m.things.length; i++) if (m.things[i].n === n) { m.things.splice(i, 1); return "the " + n + " is off your map"; }
      for (i = 0; i < m.rooms.length; i++) if (m.rooms[i].n === n) {
        m.rooms.splice(i, 1);
        m.doors = m.doors.filter(function (o) { return o.a !== n && o.b !== n; });
        m.things.forEach(function (t) { if (t.room === n) t.room = ""; });
        if (m.here.room === n) m.here.room = "";
        return "the " + n + " is off your map";
      }
      return "";
    },
    rename: function (m, op) {
      var n = name(op.rename), to = name(op.to), t = thingOf(m, n), r = roomOf(m, n);
      if (!n || !to || n === to || thingOf(m, to) || roomOf(m, to)) return "";
      if (t) { t.n = to; return "the " + n + " is the " + to + " now"; }
      if (!r) return "";
      r.n = to;
      m.doors.forEach(function (o) { if (o.a === n) o.a = to; if (o.b === n) o.b = to; });
      m.things.forEach(function (x) { if (x.room === n) x.room = to; });
      if (m.here.room === n) m.here.room = to;
      return "the " + n + " is the " + to + " now";
    },
  };

  /* One reply's `map`: an op object or a list of them, at most OPS_MAX. Returns what changed,
     in his own terms, to show him next turn. */
  function apply(m, ops, now) {
    var list = Array.isArray(ops) ? ops : [ops], said = [];
    list.slice(0, OPS_MAX).forEach(function (op) {
      if (!op || typeof op !== "object") return;
      var s = "";
      // `room` also rides on fix/saw (which room it is in), so it is its own op only alone.
      if (op.saw !== undefined) s = OPS.saw(m, op, now, false);
      else if (op.fix !== undefined || op.moved !== undefined) s = OPS.saw(m, op, now, true);
      else if (op.door !== undefined) s = OPS.door(m, op, now);
      else if (op.wall !== undefined) s = OPS.wall(m, op, now);
      else if (op.at !== undefined) s = OPS.at(m, op, now);
      else if (op.forget !== undefined) s = OPS.forget(m, op);
      else if (op.rename !== undefined) s = OPS.rename(m, op);
      else if (op.room !== undefined) s = OPS.room(m, op, now);
      if (s) said.push(s);
      m.t = now;
    });
    return said;
  }

  /* The way from his room to another: rooms joined by doorways, breadth first. */
  function route(m, from, to) {
    if (!from || !to) return null;
    if (from === to) return [];
    var prev = {}, q = [from], seenR = {};
    seenR[from] = 1;
    while (q.length) {
      var r = q.shift();
      for (var i = 0; i < m.doors.length; i++) {
        var o = m.doors[i], nx = o.a === r ? o.b : o.b === r ? o.a : "";
        if (!nx || seenR[nx]) continue;
        seenR[nx] = 1; prev[nx] = { r: r, door: o }; q.push(nx);
        if (nx === to) {
          var path = [], c = to;
          while (c !== from) { path.unshift({ door: prev[c].door, into: c }); c = prev[c].r; }
          return path;
        }
      }
    }
    return null;
  }
  function ago(t, now) {
    var s = Math.max(0, (now - t) / 1000);
    return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago";
  }
  function legs(m, path) {
    var out = [], x = m.here.x, y = m.here.y, h = m.here.h;
    path.forEach(function (p, i) {
      var dx = p.door.x - x, dy = p.door.y - y, d = Math.hypot(dx, dy), b = wrap(Math.atan2(dy, dx) / D - h);
      out.push((i ? "then " : "") + (Math.abs(b) < 12 ? "go straight" : "turn " + Math.round(Math.abs(b)) + " degrees " + (b > 0 ? "left" : "right") + " (walk:{turn:" + Math.round(b) + "})") +
        " and walk about " + r2(Math.round(d * 10) / 10) + " m to the doorway into the " + p.into);
      x = p.door.x; y = p.door.y; h = Math.atan2(dy, dx) / D;
    });
    return { text: out.join(", "), x: x, y: y, h: h };
  }
  function aim(b) { return Math.abs(b) < 12 ? "it is straight ahead" : "turn " + Math.round(Math.abs(b)) + " degrees " + (b > 0 ? "left" : "right") + " (walk:{turn:" + Math.round(b) + "}) to face it"; }

  /* His question to the map ("piano", "kitchen", "me", "map"): where it is from him, and the way
     there through the doorways. */
  function where(m, q, now) {
    var n = name(q);
    var cur = m.here.room;
    if (!m.rooms.length && !m.things.length) return "your map is empty: you have not put any room or thing on it yet.";
    if (!n || /^(me|here|i|where am i|myself|you)$/.test(n)) {
      var near = m.things.map(function (t) { return { t: t, r: relTo(m, t.x, t.y) }; }).sort(function (a, b) { return a.r.d - b.r.d; }).slice(0, 3);
      return "by your map you are " + (cur ? "in the " + cur : "somewhere not in any room you drew") +
        (near.length ? "; nearest: " + near.map(function (x) { return "the " + x.t.n + " " + far(x.r.d) + ", " + side(x.r.b); }).join("; ") : "") +
        ". your place is a guess from your walking until you reach something you know (map:{at:...}).";
    }
    if (/^(map|house|everything|all|the house|whole map)$/.test(n)) {
      return "your map: " + m.rooms.map(function (r) {
        var th = m.things.filter(function (t) { return t.room === r.n; }).map(function (t) { return t.n; });
        var ds = m.doors.filter(function (o) { return o.a === r.n || o.b === r.n; }).map(function (o) { return o.a === r.n ? o.b || "somewhere unknown" : o.a || "somewhere unknown"; });
        return "the " + r.n + (r.n === cur ? " (you are here)" : "") + ", about " + r2(Math.round(r.w * 10) / 10) + " by " + r2(Math.round(r.d * 10) / 10) + " m" +
          (th.length ? ", with " + th.join(", ") : "") + (ds.length ? ", doorways to " + ds.join(", ") : "");
      }).join("; ") + ".";
    }
    var t = thingOf(m, n) || (function () {
      // A word of the name is enough ("piano" finds "grand piano").
      for (var i = 0; i < m.things.length; i++) if ((" " + m.things[i].n + " ").indexOf(" " + n + " ") >= 0) return m.things[i];
      return null;
    })();
    var room = !t && (roomOf(m, n) || (function () {
      for (var i = 0; i < m.rooms.length; i++) if ((" " + m.rooms[i].n + " ").indexOf(" " + n + " ") >= 0) return m.rooms[i];
      return null;
    })());
    if (!t && !room) return "the " + n + " is not on your map yet.";
    var goal = t ? t.room : room.n, path = route(m, cur, goal), lead = "";
    if (t) {
      var rel = relTo(m, t.x, t.y);
      lead = "the " + t.n + (t.room ? " is in the " + t.room : " is on your map") + (t.rough ? " (you did not mark the spot, only the room)" : "") +
        ", last seen " + ago(t.t, now) + ". ";
      if (!goal || goal === cur || !path) return lead + "by your map it is " + far(rel.d) + ", " + side(rel.b) + ": " + aim(rel.b) + "." +
        (goal && goal !== cur && !path ? " you have no doorway on your map that leads there." : "");
      var L = legs(m, path), dx = t.x - L.x, dy = t.y - L.y;
      return lead + "to get there, " + L.text + ", then it is about " + r2(Math.round(Math.hypot(dx, dy) * 10) / 10) + " m further " +
        side(wrap(Math.atan2(dy, dx) / D - L.h)).replace("to your ", "to the ").replace("behind you", "back the way you came").replace("straight ahead", "straight on") + ".";
    }
    if (room.n === cur) return "you are in the " + room.n + " now.";
    if (!path) {
      var rr = relTo(m, room.x, room.y);
      return "the " + room.n + " is on your map, " + far(rr.d) + " " + side(rr.b) + ", but you have no doorway on your map that leads there from " + (cur ? "the " + cur : "here") + ".";
    }
    return "to get to the " + room.n + ": " + legs(m, path).text + ".";
  }

  /* A few lines in every turn: where he thinks he is, what is near, and what just changed. */
  /* The thing or room a goal names, when it is on the map ("find the piano" -> piano). */
  function goalTarget(m, goal) {
    var g = " " + String(goal || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ") + " ", best = "";
    m.things.concat(m.rooms).forEach(function (x) { if (g.indexOf(" " + x.n + " ") >= 0 && x.n.length > best.length) best = x.n; });
    return best;
  }
  function live(m, now, changed, goal) {
    var lines = [], cur = m.here.room;
    if (!m.rooms.length && !m.things.length) {
      lines.push("your map of the house is empty. when you see where you are, name the room (map:{room:\"living room\"}) and put what you see on it (map:{saw:\"couch\",x:0.3,m:2}).");
    } else {
      var r = cur && roomOf(m, cur), ds = [];
      if (r) m.doors.forEach(function (o) {
        if (o.a !== cur && o.b !== cur) return;
        var rel = relTo(m, o.x, o.y);
        ds.push("a doorway to " + ((o.a === cur ? o.b : o.a) || "somewhere you have not named") + " " + far(rel.d) + ", " + side(rel.b));
      });
      var near = m.things.filter(function (t) { return !cur || t.room === cur; }).map(function (t) { return { t: t, r: relTo(m, t.x, t.y) }; })
        .sort(function (a, b) { return a.r.d - b.r.d; }).slice(0, 4);
      lines.push("by your map you are " + (cur ? "in the " + cur : "outside every room you drew") + (ds.length ? "; " + ds.slice(0, 3).join("; ") : "") + ".");
      if (near.length) lines.push("near you: " + near.map(function (x) { return x.t.n + " " + far(x.r.d) + ", " + side(x.r.b); }).join("; ") + ".");
      var others = m.rooms.filter(function (x) { return x.n !== cur; }).map(function (x) { return x.n; });
      if (others.length) lines.push("other rooms on your map: " + others.slice(0, 8).join(", ") + " (where:\"kitchen\" gives the way).");
    }
    var gt = goal && goalTarget(m, goal);
    if (gt) lines.push("for your goal, your map says: " + where(m, gt, now));
    // The low camera makes everything look close (live w5: "I made it" with his own map
    // saying 1.5 m): past arm's length, the map says plainly he is not there yet.
    var gth = gt && thingOf(m, gt);
    if (gth && !gth.rough && relTo(m, gth.x, gth.y).d > 0.6) lines.push("so by your map you are not at the " + gth.n + " yet.");
    if (changed && changed.length) lines.push("you just changed your map: " + changed.join("; ") + ".");
    return "\n\n== YOUR MAP (a rough sketch you keep; where you are is a guess from your own walking) ==\n" + lines.join("\n");
  }

  /* Degrees he wants to turn, from walk:{turn} or walk:{toward}: a number of degrees, a spot
     across his picture (0..1), or a thing on his map. Positive is to his left. */
  function turnFor(m, spec) {
    if (!spec || typeof spec !== "object") return null;
    var t = num(spec.turn, -360, 360);
    if (t !== null) return t;
    var tw = spec.toward;
    if (typeof tw === "number" || (typeof tw === "string" && /^\s*[01]?(\.\d+)?\s*$/.test(tw) && tw.trim() !== "")) {
      var x = num(tw, 0, 1);
      return x === null ? null : bearingOf({ x: x });
    }
    var n = name(tw), th = n && thingOf(m, n);
    if (th) return relTo(m, th.x, th.y).b;
    return null;
  }

  /* After a step of dead reckoning: a room's walls hold him, as the real ones do, so a walk
     into a wall (which the phone cannot tell from walking) leaves him at the wall on the map,
     not metres beyond it. He leaves a room only near a doorway on it, and then he is in the
     room beyond. */
  var DOOR_PASS_M = 0.8;
  function step(m) {
    var r = m.here.room && roomOf(m, m.here.room);
    if (!r || inside(r, m.here.x, m.here.y, 0.05)) return;
    for (var i = 0; i < m.doors.length; i++) {
      var o = m.doors[i];
      if ((o.a === r.n || o.b === r.n) && Math.hypot(o.x - m.here.x, o.y - m.here.y) < DOOR_PASS_M) {
        var n = roomAt(m, m.here.x, m.here.y, o.a === r.n ? o.b : o.a);
        if (n && n !== r.n) m.here.room = n;
        return;
      }
    }
    var ix = r.w / 2 - 0.05, iy = r.d / 2 - 0.05;
    m.here.x = Math.min(r.x + ix, Math.max(r.x - ix, m.here.x));
    m.here.y = Math.min(r.y + iy, Math.max(r.y - iy, m.here.y));
  }

  var api = {
    fresh: fresh, norm: norm, apply: apply, where: where, live: live, goalTarget: goalTarget, route: route, turnFor: turnFor,
    bearingOf: bearingOf, distOf: distOf, step: step, wrap: wrap, CAM_HFOV: CAM_HFOV,
  };
  root.gbMapLib = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window === "undefined") return;

  /* ------------------------------------------------------------------ the page half */

  var changed = [], changedT = 0, CHANGED_MS = 60000, whereT = 0, WHERE_GAP_MS = 20000, checked = null;
  /* A map that came from storage, a soul file or the box is checked once, whole, before it is
     used (norm): its names go into his prompt, and a malformed entry must not break every turn.
     Each new object (a load, a merge from another tab) is checked again. */
  function soulMap() {
    var s = typeof _live !== "undefined" && _live ? _live : null;
    if (!s) return null;
    if (s.map !== checked) { s.map = norm(s.map, Date.now()); checked = s.map; }
    return s.map;
  }

  /* Heading: each orientation reading's turn about the vertical, from the whole rotation matrix
     (the page's _stripRot/_stripYaw do the same for the felt strip), so it holds with the phone
     standing upright. Turns add up into here.h, so a page reload carries on from the stored
     heading, as if he had not turned while it loaded. */
  var prevR = null;
  function rot(a, b, g) {
    var ca = Math.cos(a * D), sa = Math.sin(a * D), cb = Math.cos(b * D), sb = Math.sin(b * D), cg = Math.cos(g * D), sg = Math.sin(g * D);
    return [ca * cg - sa * sb * sg, -sa * cb, ca * sg + sa * sb * cg, sa * cg + ca * sb * sg, ca * cb, sa * sg - ca * sb * cg];
  }
  // The turn between two readings about the world's vertical, + counter-clockwise from above.
  function yawStep(p, c) {
    var d00 = c[0] * p[0] + c[1] * p[1] + c[2] * p[2], d10 = c[3] * p[0] + c[4] * p[1] + c[5] * p[2];
    return Math.atan2(d10, d00) / D;
  }
  var turnWatch = null;
  window.addEventListener("deviceorientation", function (e) {
    try {
      if (e.alpha == null || e.beta == null || e.gamma == null) return;
      var c = rot(e.alpha, e.beta, e.gamma), m;
      if (prevR) {
        var dy = yawStep(prevR, c);
        // More than 45 degrees in one reading is the sensor re-referencing, not a turn.
        if (Math.abs(dy) <= 45 && spinW) { spinW.turned += dy; spinW.n++; }
        if (Math.abs(dy) <= 45 && (m = soulMap())) {
          m.here.h = wrap(m.here.h + dy);
          if (turnWatch) watchTurn(dy);
        }
      }
      prevR = c;
    } catch (err) {}
  });

  /* Position: his own walk carries him along his heading at about his trained walk's pace. The
     sim measured 0.03 to 0.05 m/s for the trained forward (2026-11-17); a wall or a slip makes
     this an over-guess, which reaching a known thing corrects. */
  var PACE = { forward: 0.04, back: 0.03 }, lastTick = 0;
  // A spin runs as a "walk" motion too; its move report says which it is.
  function mv(M) { return (M && M.report && M.report.mv) || {}; }
  function spinning(M) { return !!M && M.kind === "walk" && mv(M).kind === "spin"; }
  function walking() {
    var M = typeof _motion !== "undefined" ? _motion : null;
    if (!M || M.loading || M.kind !== "walk") return "";
    if (M.own) return M.own;
    // What physically ran, as the page's own move line reads it: the official gait walks
    // screen-backward (away from where his camera looks) even when asked "fwd", and a turn
    // carries him nowhere.
    var v = mv(M);
    if (v.kind !== "walk" || v.direction === "turn") return "";
    if (v.direction === "screen-forward") return "forward";
    if (v.direction === "screen-backward" || v.gait === "official") return "back";
    return "";
  }
  /* A spin the gyro says went nowhere: something presses on his side (mf8 spun 127 times
     wedged between the lamp and the wall, every one "done", his heading unmoved for ten
     minutes). A spin can do nothing there; walking back out can. */
  var spinW = null, stuckSpinT = 0, SPIN_STUCK_MS = 2500, SPIN_STUCK_DEG = 8;
  function watchSpin(now) {
    // Timed from when the spin's policy has loaded and its legs move, and only with gyro
    // readings to go by; a spin the gyro stopped as asked turned, however little.
    var M = typeof _motion !== "undefined" ? _motion : null, sp = spinning(M) && !M.loading;
    if (sp && (!spinW || spinW.rep !== M.report)) spinW = { rep: M.report, t0: now, turned: 0, n: 0 };
    else if (!sp && spinW) {
      if (now - spinW.t0 >= SPIN_STUCK_MS && spinW.n > 0 && !(spinW.rep && spinW.rep._gbTurned) && Math.abs(spinW.turned) < SPIN_STUCK_DEG) {
        stuckSpinT = now;
        try { logEv("spin_stuck", { ms: now - spinW.t0, deg: Math.round(spinW.turned) }); } catch (e) {}
      }
      spinW = null;
    }
  }
  setInterval(function () {
    try {
      var now = Date.now(), dt = lastTick ? Math.min(1, (now - lastTick) / 1000) : 0;
      lastTick = now;
      watchSpin(now);
      var w = walking(), m;
      if (!w || !dt || !(m = soulMap())) return;
      var v = (w === "back" ? -PACE.back : PACE.forward) * dt, a = m.here.h * D;
      m.here.x += v * Math.cos(a); m.here.y += v * Math.sin(a); m.here.t = now;
      step(m);
    } catch (err) {}
  }, 250);


  /* A turn by degrees: the spin he asked for, stopped by the gyro once it has turned that far,
     since a spin's own pace changes with the floor. The time asked is long enough that the
     gyro, not the clock, ends it: over 684 turns on 2026-11-18 only 131 reached their angle
     at 25 degrees a second, and the rest turned at a median of 6. */
  var SPIN_DEG_S = 8, STOP_EARLY = 6;
  function watchTurn(dy) {
    var w = turnWatch, M = typeof _motion !== "undefined" ? _motion : null;
    // Only the spin this turn started is ever stopped: once it ends or another move takes the
    // legs, the watch lapses.
    if (w.M === undefined) {
      // The spin's placeholder motion is replaced once its policy loads, so the watch holds
      // the move's own report, which both carry.
      if (spinning(M)) w.M = M.report;
      else if (Date.now() - w.t0 < 3000) { w.done += dy * w.sign; return; }
      else w.M = null;
    }
    w.done += dy * w.sign;
    // No spin seen in its first 3 s (w.M null) is lapsed: a reportless glide must not match.
    var lapsed = !M || !w.M || M.report !== w.M || Date.now() > w.until;
    if (lapsed || w.done >= w.want - STOP_EARLY) {
      turnWatch = null;
      try { logEv("map_turn", { want: Math.round(w.want * w.sign), did: Math.round(w.done * w.sign), stop: lapsed ? 0 : 1 }); } catch (e) {}
      // The turn ended as asked: the stop's new motion generations are marked done, so a
      // thought already in flight is not refused as overtaken by it (motion_refused).
      // The stop marks the spin's report as turned as asked, so the explore trip does not read
      // its "cancelled" as a person's stop.
      if (!lapsed) try { if (M && M.report) M.report._gbTurned = 1; } catch (e) {}
      if (!lapsed) try { if (typeof runGait === "function") { var g0 = typeof _motionGen === "number" ? _motionGen : null; runGait({ dir: "stop" }); if (g0 !== null && typeof _gbDoneGen === "object") for (var g = g0 + 1; g <= _motionGen; g++) _gbDoneGen[g] = 1; } } catch (e) {}
    }
  }
  function turnSpec(spec) {
    var m = soulMap(), deg = m && turnFor(m, spec);
    if (deg === null || deg === undefined || !isFinite(deg)) {
      // A turn toward something not on his map (or not a number) is no move; say so in the log.
      try { logEv("map_turn", { want: null, why: "unknown" }); } catch (e) {}
      return null;
    }
    deg = wrap(deg);
    if (Math.abs(deg) < 8) { try { logEv("map_turn", { want: Math.round(deg), did: 0 }); } catch (e) {} return null; }
    var left = deg > 0, secs = Math.min(15, Math.max(1.5, Math.abs(deg) / SPIN_DEG_S * 1.3));
    turnWatch = { want: Math.abs(deg), sign: left ? 1 : -1, done: 0, t0: Date.now(), M: undefined, until: Date.now() + secs * 1000 + 4000 };
    return { gait: left ? "spin_left" : "spin_right", dir: left ? "left" : "right", secs: Math.round(secs * 10) / 10, gain: 0.99 };
  }

  // The line he says while he looks (the page's gbToolLine), as the other tools have.
  function toolLines() {
    try {
      if (typeof GB_TOOL_LINES === "undefined" || GB_TOOL_LINES.where) return;
      GB_TOOL_LINES.where = ["Let me check my map.", "One moment, let me look at my map.", "Let me think where that is."];
      GB_TOOL_SAY_KEY.where = /\b(map|where|think|check|checking|look|looking)\b/;
      ["map", "my", "where", "think", "thinking", "is"].forEach(function (w) { GB_TOOL_SAY_OK[w] = 1; });
    } catch (e) {}
  }
  window.gbMap = {
    /* From the reply handler (apply.py MAP_PATCHES), before the walk is dispatched. */
    reply: function (o, kind, extra) {
      var m = soulMap(), asked = false;
      toolLines();
      if (!m || !o || typeof o !== "object") return false;
      if (o.map && typeof o.map === "object") {
        var said = apply(m, o.map, Date.now());
        if (said.length) { changed = changed.concat(said).slice(-4); changedT = Date.now(); }
        try { logEv("map", { said: said, rooms: m.rooms.length, things: m.things.length }); } catch (e) {}
        try { if (typeof gbToolPill === "function") gbToolPill("map"); } catch (e) {}
      }
      if (o.walk && typeof o.walk === "object" && (o.walk.turn !== undefined || o.walk.toward !== undefined)) {
        var s = turnSpec(o.walk);
        if (s === null) delete o.walk; else o.walk = s;
      }
      if (typeof o.where === "string" && name(o.where)) {
        // A follow-up never asks again (it cannot loop), and his own beats ask at most every
        // WHERE_GAP_MS: the map's lines in every turn already say what is near.
        var why = extra && extra.recallFollow ? "follow" : kind !== "event" && Date.now() - whereT < WHERE_GAP_MS ? "gap" : "";
        if (why) { try { logEv("where_skip", { why: why }); } catch (e) {} return false; }
        whereT = Date.now();
        var found = "you looked at your map for \"" + name(o.where) + "\": " + where(m, o.where, Date.now());
        try { logEv("where", { q: name(o.where) }); } catch (e) {}
        asked = typeof gbToolFollow === "function" && gbToolFollow("where", found,
          "you looked at your map (what it says is under WHAT YOU JUST LOOKED UP). now act on it or tell your person, in your own voice; it is a rough sketch, so look with your camera too.");
      }
      return asked;
    },
    /* The turn's live block. */
    live: function (legs) {
      var m = soulMap();
      if (!m || (!legs && !m.rooms.length && !m.things.length)) return "";
      if (Date.now() - changedT > CHANGED_MS) changed = [];
      var s = live(m, Date.now(), changed, typeof seekGoal === "string" ? seekGoal : "");
      changed = [];
      return s;
    },
    map: function () { return soulMap(); },
    // The spin that stops at the angle, for a turn the explore trip makes between his thoughts.
    turn: function (deg) { return turnSpec({ turn: deg }); },
    // When the last spin that went nowhere ended (ms since epoch), or 0.
    stuckSpinT: function () { return stuckSpinT; },
    /* How far, by his map, the thing his goal names is (metres), or null when it isn't on
       the map with a spot. */
    goalDist: function (goal) {
      var m = soulMap(), t = m && goalTarget(m, goal), th = t && thingOf(m, t);
      return th && !th.rough ? relTo(m, th.x, th.y).d : null;
    },
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
