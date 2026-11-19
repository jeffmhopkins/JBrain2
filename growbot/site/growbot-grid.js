/* Dave's map of where walls and floor are, and the planner that explores it (docs/plans/
   GROWBOT_EXPLORE_PLAN.md, wave E2). His depth sense (growbot-vision.js) hands each frame's
   scan here: per direction, the nearest thing at his height, or clear as far as the picture
   reaches. On a pose that is measured (gbPose fed), each scan's rays free the cells they cross
   and mark the cell they end on, so the house builds up as a 5 cm occupancy grid, and what he
   saw from across a room stays known as he closes in (no range sensor, owner).

   Exploring is then frontiers: the free cells next to cells never seen. He goes to the nearest
   one by the planner's path, looks round, and goes on, until none he can reach is left. The
   planner only plans through cells seen free, keeps his middle BODY_M off anything seen, and
   prefers the middle of a passage. A route that ends anywhere but its goal ("stuck") leaves
   that frontier off his list, so a wall the scans missed costs one try.

   Labelled things in view (sofa, fridge, ...) are anchored where they stand, for the inspection
   phase and his answers. The grid lives in the page for now; the soul keeps the rooms sketch.
   The pure half is tested in node; the page half reads the creature page's globals lazily. */
(function (root) {
  "use strict";
  var D = Math.PI / 180;
  var CELL = 0.05;
  // A depth model's far readings are its worst: past this a hit only says "something out there".
  var RANGE_M = 3.5;
  // Log-odds in small integers: a hit counts three misses, so one glimpse of a wall outweighs
  // the rays that grazed past it, and a cell must be seen free twice before it is planned on.
  var L_FREE = -1, L_OCC = 3, L_MIN = -10, L_MAX = 15, OCC_AT = 3, FREE_AT = -2;
  // His body is about 20 cm across: his middle stays this far off anything, and within CLEAR_M
  // a cell costs more, so a route takes the middle of a doorway rather than its jamb.
  var BODY_M = 0.15, CLEAR_M = 0.35, NEAR_COST = 4, TIGHT_COST = 25;
  // A frontier smaller than this is a gap between two rays, not a way on.
  var FRONTIER_MIN = 8;
  // Each direction is a few degrees wide: its free rays fan across it, so a far wall's cells
  // between two directions are not left unseen.
  var FAN = 3;
  // A goal tried and not reached keeps frontiers this near it off the list.
  var FAILED_M = 0.4;
  var THING_JOIN_M = 0.75;
  // Looking round: headings tried, the camera's view (about 60 degrees, and what the depth
  // sense reaches), and how many unseen cells make a look worth its turn (about 0.1 m2).
  var LOOK_STEP = 30, VIEW_DEG = 56, VIEW_M = 2.5, LOOK_WORTH = 40;
  // His camera's floor starts about this far out: nearer than it no look shows, so it is
  // neither scored by a look nor gone to as a frontier.
  var FEET_M = 0.4;
  var GO_POINTS_MAX = 64;
  // His phone's lens sits this far ahead of his middle (the Dave sim measured 8 cm: a wall
  // 40 cm from his middle reads 32).
  var CAM_AHEAD_M = 0.08;

  function key(i, j) { return (i + 32768) * 65536 + (j + 32768); }
  function ij(k) { return [Math.floor(k / 65536) - 32768, (k % 65536) - 32768]; }
  function cellOf(x, y) { return [Math.floor(x / CELL), Math.floor(y / CELL)]; }
  function mid(i, j) { return { x: (i + 0.5) * CELL, y: (j + 0.5) * CELL }; }

  function Grid() { this.l = new Map(); this.things = []; this.scans = 0; }

  Grid.prototype.at = function (i, j) {
    var v = this.l.get(key(i, j)) || 0;
    return v >= OCC_AT ? 1 : v <= FREE_AT ? -1 : 0;
  };
  Grid.prototype._bump = function (k, d) {
    this.l.set(k, Math.max(L_MIN, Math.min(L_MAX, (this.l.get(k) || 0) + d)));
  };

  /* One scan from pose ({x, y, h}: metres, camera heading in degrees CCW from +x). Each
     direction's ray frees the cells it crosses and marks the one it ends on; a clear direction
     frees as far as the picture reached (`reach`). A cell hit by any ray is not freed by
     another in the same scan. */
  Grid.prototype.add = function (pose, scan, reach) {
    var free = new Set(), occ = new Set();
    var width = scan.length > 1 ? Math.abs(scan[1].deg - scan[0].deg) : 0;
    for (var c = 0; c < scan.length; c++) {
      if (scan[c].blind) continue;
      var col = scan[c], hit = col.m !== null && col.m <= RANGE_M;
      // A clear direction frees as far as its own floor was seen (`open`), or, from a source
      // that gives none, as far as the picture reached.
      var r = Math.min(col.m === null ? (col.open !== undefined ? +col.open || 0 : +reach || 0) : col.m, RANGE_M);
      for (var f = 0; f < FAN; f++) {
        var off = width * ((f + 0.5) / FAN - 0.5), a = (pose.h + col.deg + off) * D, ca = Math.cos(a), sa = Math.sin(a);
        // The middle ray right up to the hit: the cell it lands in is taken back out below, and
        // stopping short would leave an unseen seam along the wall, a frontier that leads
        // nowhere. The side rays stop a cell short: on a wall met at a slant, the direction's
        // nearest reading lies past the wall's face along them.
        var end = hit && Math.abs(off) > 1e-9 ? r - CELL : r;
        for (var s = 0; s < end; s += CELL / 2) {
          var p = cellOf(pose.x + ca * s, pose.y + sa * s);
          free.add(key(p[0], p[1]));
        }
      }
      if (hit) {
        var a0 = (pose.h + col.deg) * D, e = cellOf(pose.x + Math.cos(a0) * col.m, pose.y + Math.sin(a0) * col.m);
        occ.add(key(e[0], e[1]));
      }
    }
    var self = this;
    occ.forEach(function (k) { free.delete(k); });
    free.forEach(function (k) { self._bump(k, L_FREE); });
    occ.forEach(function (k) { self._bump(k, L_OCC); });
    this.scans++;
  };

  /* A labelled thing seen at deg, m from pose: joined to one of its kind already near there
     (its spot the running mean of its sightings), or a new one. */
  Grid.prototype.saw = function (pose, what, deg, m) {
    var a = (pose.h + deg) * D, x = pose.x + Math.cos(a) * m, y = pose.y + Math.sin(a) * m;
    for (var i = 0; i < this.things.length; i++) {
      var t = this.things[i];
      if (t.what === what && Math.hypot(t.x - x, t.y - y) < THING_JOIN_M) {
        t.x += (x - t.x) / (t.n + 1); t.y += (y - t.y) / (t.n + 1); t.n++;
        return t;
      }
    }
    var nt = { what: what, x: x, y: y, n: 1 };
    this.things.push(nt);
    return nt;
  };

  /* Each seen cell's distance to the nearest occupied one, in cells, up to the clearance
     radius; cells further off are absent. */
  Grid.prototype._clearance = function () {
    var R = Math.ceil(CLEAR_M / CELL), offs = [], out = new Map();
    for (var di = -R; di <= R; di++) for (var dj = -R; dj <= R; dj++) {
      var dd = Math.hypot(di, dj);
      if (dd <= R) offs.push([di, dj, dd]);
    }
    this.l.forEach(function (v, k) {
      if (v < OCC_AT) return;
      var c = ij(k);
      for (var o = 0; o < offs.length; o++) {
        var kk = key(c[0] + offs[o][0], c[1] + offs[o][1]), was = out.get(kk);
        if (was === undefined || offs[o][2] < was) out.set(kk, offs[o][2]);
      }
    });
    return out;
  };

  /* Cheapest paths from (x, y) over cells seen free, each step's cost raised near anything
     seen and steeply where his body would touch it. Unseen cells within two of his own are
     allowed, since he is standing there; a cell seen occupied never is. Returns {dist, prev}
     keyed by cell. */
  Grid.prototype.paths = function (x, y) {
    var clear = this._clearance(), body = BODY_M / CELL, nearR = CLEAR_M / CELL;
    var s = cellOf(x, y), sk = key(s[0], s[1]), dist = new Map(), prev = new Map(), heap = new Heap();
    var self = this;
    function stepCost(i, j) {
      var st = self.at(i, j);
      if (st === 1) return null;
      if (st === 0 && (Math.abs(i - s[0]) > 2 || Math.abs(j - s[1]) > 2)) return null;
      var d = clear.get(key(i, j));
      if (d === undefined) return 1;
      if (d <= body) return TIGHT_COST;
      return 1 + NEAR_COST * (nearR - d) / nearR;
    }
    dist.set(sk, 0); heap.push(0, sk);
    while (heap.size()) {
      var top = heap.pop(), k = top[1], dk = top[0];
      if (dk > dist.get(k)) continue;
      var c = ij(k);
      for (var di = -1; di <= 1; di++) for (var dj = -1; dj <= 1; dj++) {
        if (!di && !dj) continue;
        var ni = c[0] + di, nj = c[1] + dj, w = stepCost(ni, nj);
        if (w === null) continue;
        // No corner-cutting past an occupied cell.
        if (di && dj && (self.at(c[0] + di, c[1]) === 1 || self.at(c[0], c[1] + dj) === 1)) continue;
        var nk = key(ni, nj), nd = dk + w * (di && dj ? Math.SQRT2 : 1) * CELL;
        if (!(nd >= dist.get(nk))) { dist.set(nk, nd); prev.set(nk, k); heap.push(nd, nk); }
      }
    }
    return { dist: dist, prev: prev, clear: clear, from: sk };
  };

  /* Groups of free cells next to cells never seen (4-neighbours), 8-connected, at least
     FRONTIER_MIN cells. */
  Grid.prototype.frontiers = function () {
    var l = this.l, edge = new Set();
    // Not yet seen: never, or only glimpsed free once. A cell any ray has ended on is a
    // wall's edge, not a way on.
    var never = function (i, j) { var v = l.get(key(i, j)); return v === undefined || v > FREE_AT && v <= 0; };
    l.forEach(function (v, k) {
      if (v > FREE_AT) return;
      var c = ij(k);
      if (never(c[0] + 1, c[1]) || never(c[0] - 1, c[1]) || never(c[0], c[1] + 1) || never(c[0], c[1] - 1)) edge.add(k);
    });
    var seen = new Set(), out = [];
    edge.forEach(function (k0) {
      if (seen.has(k0)) return;
      var group = [], todo = [k0];
      seen.add(k0);
      while (todo.length) {
        var k = todo.pop(), c = ij(k);
        group.push(k);
        for (var di = -1; di <= 1; di++) for (var dj = -1; dj <= 1; dj++) {
          var nk = key(c[0] + di, c[1] + dj);
          if (edge.has(nk) && !seen.has(nk)) { seen.add(nk); todo.push(nk); }
        }
      }
      if (group.length >= FRONTIER_MIN) out.push(group);
    });
    return out;
  };

  /* The nearest frontier he can reach, by path: {goal: {x, y}, cells, m, points}, or null. A
     frontier within FAILED_M of a goal in `failed` is skipped. */
  Grid.prototype.next = function (x, y, failed) {
    var P = this.paths(x, y), best = null;
    var fronts = this.frontiers();
    for (var f = 0; f < fronts.length; f++) {
      var g = null;
      for (var q = 0; q < fronts[f].length; q++) {
        var d = P.dist.get(fronts[f][q]);
        if (d === undefined || g && d >= g.d) continue;
        // The cells at his feet are the edges of each look's wedge, which start at his lens: a
        // frontier reaching there is aimed at by its nearest cell past them, not dropped.
        var cq = ij(fronts[f][q]), mq = mid(cq[0], cq[1]);
        if (Math.hypot(mq.x - x, mq.y - y) < FEET_M) continue;
        g = { d: d, k: fronts[f][q] };
      }
      if (!g) continue;
      var c = ij(g.k), at = mid(c[0], c[1]);
      if ((failed || []).some(function (b) { return Math.hypot(b.x - at.x, b.y - at.y) < FAILED_M; })) continue;
      if (!best || g.d < best.m) best = { goal: at, cells: fronts[f].length, m: g.d, k: g.k };
    }
    if (!best) return null;
    best.points = this.route(P, best.k);
    best.m = Math.round(best.m * 100) / 100;
    delete best.k;
    return best;
  };

  /* The path to cell k as few points as keep it on seen floor his body clears: from each
     point, the furthest cell down the path in a straight line over such cells. A path that
     needs more than GO_POINTS_MAX keeps its first ones: he plans again where they end. */
  Grid.prototype.route = function (P, k) {
    var cells = [];
    for (var c = k; c !== undefined; c = P.prev.get(c)) cells.unshift(c);
    var self = this, body = BODY_M / CELL;
    function ok(k1) {
      var c1 = ij(k1), d = P.clear.get(k1);
      return self.at(c1[0], c1[1]) === -1 && (d === undefined || d > body);
    }
    function sight(a, b) {
      var A = ij(a), B = ij(b), n = Math.max(Math.abs(B[0] - A[0]), Math.abs(B[1] - A[1])) * 2;
      for (var t = 1; t < n; t++) {
        var i = Math.round(A[0] + (B[0] - A[0]) * t / n), j = Math.round(A[1] + (B[1] - A[1]) * t / n);
        if (!ok(key(i, j))) return false;
      }
      return true;
    }
    var pts = [], at = 0;
    while (at < cells.length - 1) {
      var far = at + 1;
      for (var z = cells.length - 1; z > at + 1; z--) if (sight(cells[at], cells[z])) { far = z; break; }
      var cc = ij(cells[far]);
      pts.push(mid(cc[0], cc[1]));
      at = far;
    }
    return pts.slice(0, GO_POINTS_MAX).map(function (p) { return { x: Math.round(p.x * 100) / 100, y: Math.round(p.y * 100) / 100 }; });
  };

  /* Which way to look from (x, y): of headings every LOOK_STEP degrees, the one whose view
     (a VIEW_DEG wedge out to VIEW_M, past his own feet) holds the most cells not yet seen, as
     {h, n}. A wall seen in a direction hides what is behind it, so a ray stops at one. */
  Grid.prototype.bestLook = function (x, y) {
    var best = { h: 0, n: -1 }, l = this.l;
    for (var h = 0; h < 360; h += LOOK_STEP) {
      var seen = new Set(), n = 0;
      for (var a = -VIEW_DEG / 2; a <= VIEW_DEG / 2; a += 2) {
        var ca = Math.cos((h + a) * D), sa = Math.sin((h + a) * D);
        for (var s = FEET_M; s < VIEW_M; s += CELL) {
          var c = cellOf(x + ca * s, y + sa * s), k = key(c[0], c[1]), v = l.get(k);
          if (v !== undefined && v >= OCC_AT) break;
          if (seen.has(k)) continue;
          seen.add(k);
          if (v === undefined || v > FREE_AT && v <= 0) n++;
        }
      }
      if (n > best.n) best = { h: h, n: n };
    }
    return best;
  };

  Grid.prototype.counts = function () {
    var f = 0, o = 0;
    this.l.forEach(function (v) { if (v <= FREE_AT) f++; else if (v >= OCC_AT) o++; });
    return { free: f, occ: o, scans: this.scans };
  };

  function Heap() { this.a = []; }
  Heap.prototype.size = function () { return this.a.length; };
  Heap.prototype.push = function (p, v) {
    var a = this.a, i = a.length;
    a.push([p, v]);
    while (i > 0) { var u = (i - 1) >> 1; if (a[u][0] <= a[i][0]) break; var t = a[u]; a[u] = a[i]; a[i] = t; i = u; }
  };
  Heap.prototype.pop = function () {
    var a = this.a, top = a[0], last = a.pop();
    if (a.length) {
      a[0] = last;
      for (var i = 0; ;) {
        var l = 2 * i + 1, r = l + 1, m = i;
        if (l < a.length && a[l][0] < a[m][0]) m = l;
        if (r < a.length && a[r][0] < a[m][0]) m = r;
        if (m === i) break;
        var t = a[m]; a[m] = a[i]; a[i] = t; i = m;
      }
    }
    return top;
  };

  var api = { Grid: Grid, CELL: CELL, RANGE_M: RANGE_M, BODY_M: BODY_M };

  if (typeof window !== "undefined" && typeof document !== "undefined") {
    var grid = new Grid();
    var fed = function () { return !!(window.gbPose && window.gbPose.source && window.gbPose.source() === "fed"); };
    var here = function () {
      try { var m = window.gbMap && window.gbMap.map && window.gbMap.map(); return m && m.here; } catch (e) { return null; }
    };
    api.grid = function () { return grid; };
    api.clear = function () { grid = new Grid(); };
    /* A frame's scene, from the depth sense. Only on a measured pose: a reckoned one would
       smear every wall along the walk's error (E4 brings the estimated pose). */
    api.see = function (sc) {
      try {
        var h = here();
        if (!sc || !sc.scan || !h || !fed()) return;
        var pose;
        if (sc.lens && [sc.lens.x, sc.lens.y, sc.lens.h].every(function (v) { return v !== null && isFinite(+v); })) {
          // A source that knows where its lens was for this frame (the sim's truth).
          pose = { x: +sc.lens.x, y: +sc.lens.y, h: +sc.lens.h };
        } else {
          // His pose is his middle; the scan is from his lens, CAM_AHEAD_M in front of it.
          var hh = sc.h === null || sc.h === undefined ? +h.h : +sc.h;
          pose = { x: +h.x + CAM_AHEAD_M * Math.cos(hh * D), y: +h.y + CAM_AHEAD_M * Math.sin(hh * D), h: hh };
        }
        if (!isFinite(pose.x) || !isFinite(pose.y) || !isFinite(pose.h)) return;
        grid.add(pose, sc.scan, sc.reach);
        (sc.things || []).forEach(function (t) { grid.saw(pose, t.what, t.deg, t.m); });
      } catch (e) {}
    };

    /* The explorer: look round, go to the nearest frontier, look round, until none is left.
       Each beat does one thing and only when his legs are free. */
    var LOOK_DEG = 90, LOOKS = 4, BEAT_MS = 500, GO_SETTLE_MS = 3000, BACK_S = 6;
    // A route that ends some other way than stuck or too long (the legs' duty budget, another
    // move taking over) is tried once more; TRIES_MAX bounds a run that keeps finding edges.
    var NO_POSE_MS = 10000, TRIES_MAX = 300;
    var ex = null, timer = null;
    var busy = function () {
      try { return typeof _motion !== "undefined" && !!_motion; } catch (e) { return false; }
    };
    var log = function (ev, d) { try { logEv(ev, d); } catch (e) {} };
    function beat() {
      if (!ex || ex.done) return;
      if (ex.phase === "go") {
        var st = window.gbMap.goState();
        // A route's walk can still be loading its skills when it is asked: not ended yet.
        if (!st || st.why === "" || st.why === "ended" && Date.now() - ex.goT < GO_SETTLE_MS) return;
        var near = function (L) {
          return L.some(function (q) { return Math.hypot(q.x - ex.goal.x, q.y - ex.goal.y) < FAILED_M; });
        };
        if (st.why === "arrived") {
          // Arriving at the same edge a second time means looking from there doesn't clear it:
          // without this he can go back and forth between two such edges for the whole run.
          if (near(ex.arrived)) ex.failed.push(ex.front); else ex.arrived.push(ex.goal);
        } else if (st.why === "stuck" || st.why === "too_long") ex.failed.push(ex.front);
        else {
          // A walk cut off on the way (the legs' duty budget, another move) that got him nearer
          // is progress; only one that ends twice without headway gives the goal up.
          var hw = here(), left = hw ? Math.hypot(+hw.x - ex.goal.x, +hw.y - ex.goal.y) : ex.goM;
          if (ex.goM - left < 0.1) { if (near(ex.ended)) ex.failed.push(ex.front); else ex.ended.push(ex.goal); }
        }
        log("grid_reached", { why: st.why, to: ex.goal });
        ex.phase = "look"; ex.looks = 0;
        // Stuck is something he can't see holding him (a jamb at his shoulder, a chair leg):
        // a few steps back free him before he looks and plans again.
        if (st.why === "stuck") ex.phase = "back";
        return;
      }
      if (busy()) return;
      if (ex.phase === "back") {
        ex.phase = "look";
        log("grid_back", {});
        if (typeof runGait === "function") runGait({ dir: "back", secs: BACK_S });
        return;
      }
      var h = here();
      if (!h || !fed()) {
        // The grid only fills on a fed pose: without one there is nothing to explore on.
        if (!ex.waitT) ex.waitT = Date.now();
        // Only before he has started: a gap in the pose mid-run is waited out.
        else if (!ex.started && Date.now() - ex.waitT > NO_POSE_MS) { ex.done = "no_pose"; log("grid_explored", { done: ex.done }); }
        return;
      }
      ex.waitT = 0;
      if (ex.phase === "look") {
        // The first look is all the way round; after that he turns only toward what he has
        // not seen, while a turn still shows enough of it, up to LOOKS turns.
        var want = null;
        if (!ex.started) want = LOOK_DEG;
        else if (ex.looks < LOOKS) {
          var b = grid.bestLook(+h.x, +h.y);
          if (b.n >= LOOK_WORTH) want = ((b.h - +h.h) % 360 + 540) % 360 - 180;
          log("grid_look", { h: b.h, n: b.n, turn: want === null ? null : Math.round(want) });
        }
        if (want !== null && ex.looks < LOOKS) {
          ex.looks++;
          var spec = window.gbMap.turn(want);
          if (spec && typeof runGait === "function") runGait(spec);
          // A turn too small to make is a look already taken.
          if (spec) return;
        }
        if (want !== null && ex.looks < LOOKS) return;
        ex.started = true;
        ex.phase = "plan";
      }
      var nx = ex.tries < TRIES_MAX ? grid.next(+h.x, +h.y, ex.failed) : null, c = grid.counts();
      if (!nx) {
        ex.done = ex.tries < TRIES_MAX ? "explored" : "tries";
        log("grid_explored", { free: c.free, occ: c.occ, things: grid.things.length, tries: ex.tries });
        return;
      }
      ex.tries++;
      log("grid_go", { to: nx.goal, m: nx.m, n: nx.points.length, free: c.free });
      // A route cut to its first points ends short of the frontier: that end is what he goes to
      // and is judged by (goal), while giving up gives up the frontier itself (front).
      var goal = nx.points[nx.points.length - 1] || nx.goal;
      if (window.gbMap.go(nx.points)) {
        ex.phase = "go"; ex.goal = goal; ex.front = nx.goal; ex.goT = Date.now(); ex.goM = Math.hypot(goal.x - +h.x, goal.y - +h.y);
      }
      else { ex.failed.push(nx.goal); ex.phase = "look"; ex.looks = LOOKS; }
    }
    /* On: explore from here. Off: stop after the current move. */
    api.explore = function (on) {
      if (timer) { clearInterval(timer); timer = null; }
      if (!on) { ex = null; return null; }
      ex = { phase: "look", looks: 0, failed: [], ended: [], arrived: [], tries: 0, done: "", goal: null, started: false, waitT: 0 };
      timer = setInterval(function () { try { beat(); } catch (e) { log("grid_error", { e: String(e).slice(0, 120) }); } }, BEAT_MS);
      return api.state();
    };
    // The grid's seen cells, for a picture of it (the sim's plan): free and occupied as
    // [i, j] pairs of CELL-sized cells.
    api.cells = function () {
      var free = [], occ = [];
      grid.l.forEach(function (v, k) {
        if (v <= FREE_AT) free.push(ij(k)); else if (v >= OCC_AT) occ.push(ij(k));
      });
      return { cell: CELL, free: free, occ: occ, things: api.state().things };
    };
    api.state = function () {
      var c = grid.counts();
      return { free: c.free, occ: c.occ, scans: c.scans, things: grid.things.map(function (t) { return { what: t.what, x: Math.round(t.x * 100) / 100, y: Math.round(t.y * 100) / 100, n: t.n }; }),
        explore: ex ? { phase: ex.phase, tries: ex.tries, failed: ex.failed.length, done: ex.done } : null };
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.gbGrid = api;
})(typeof window !== "undefined" ? window : this);
