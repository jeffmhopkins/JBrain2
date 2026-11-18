/* Dave's depth sense (owner, 2026-11-18: "phone based vision models that could do a middle
   layer of processing that would identify walls and objects etc in real time", and "how to
   abstract that for the sim is important too"). A layer between his camera and his thoughts
   that says how far things are, which way is open, and whether a walk is getting him anywhere,
   so the model picks WHERE from facts and code does HOW.

   One interface, two sources. A source hands feed() a pair of small maps of one camera frame:
   depth (metres along the camera's axis, per pixel) and labels (an index into VOCAB, per pixel),
   with the camera's pitch and height. On the phone, models make them (docs/plans/
   GROWBOT_VISION_PLAN.md, V2); in the Dave simulator, MuJoCo renders the true depth and which
   object each pixel is, with phone-like noise added (growbot/sim/dave_sim/vision.py). Everything
   from the maps on, reduce() and what the page does with a scene, is this one file in both, so
   a sim run tests the real logic and the sim's blank walls stop mattering.

   His camera sits about 10 cm off the floor and looks 31 degrees up in his tall stance (47 on
   neutral legs), so the floor shows only from about 0.9 m out: "where the floor ends" cannot
   see the half metre that matters. What can is each
   pixel's place in the room (from its depth, the pitch and the lens height): how far away it is
   across the floor, which way, and how high. Anything below HMAX is something he would walk
   into; the nearest of those in each direction is how far he can go that way.

   With no source feeding it (a phone today), nothing here changes what he does: every reader
   asks fresh() first. The pure half is tested in node; the page half reads the creature page's
   globals lazily. */
(function (root) {
  "use strict";
  var D = Math.PI / 180;
  // The words a source labels pixels with. A source maps its own classes onto these.
  var VOCAB = ["unknown", "floor", "wall", "ceiling", "door", "person", "animal", "sofa", "chair",
    "table", "shelf", "lamp", "plant", "piano", "fridge", "counter", "bed", "bench", "cabinet",
    "screen", "self"];
  // Not things: the room itself, and his own body at the picture's edge.
  var PLAIN = { unknown: 1, floor: 1, wall: 1, ceiling: 1, door: 1, self: 1 };
  // Taller than his body (about 25 cm standing) plus a margin: what he would walk into. A table
  // top or a lamp shade above this does not stop him; its legs and base do.
  var HMAX = 0.5, HMIN = 0.03;
  // Nearer than this to the lens is his own nose or leg at the picture's edge, whatever a
  // model calls it.
  var SELF_M = 0.05;
  var BINS = 9, NEAR_PCT = 0.1, OPEN_GAIN_M = 0.8, THING_SHARE = 0.015, THINGS_MAX = 5;
  // A scene this old says nothing about now.
  var FRESH_MS = 1500;
  // Pushing, not walking: walking forward this long with what is ahead this close and getting
  // no closer. At his 4 cm a second, 6 s of walking closes about 24 cm.
  var PUSH_MS = 6000, PUSH_NEAR_M = 0.7, PUSH_GAIN_M = 0.04, TOUCH_M = 0.12;

  function pct(a, p) {
    if (!a.length) return null;
    var s = a.slice().sort(function (x, y) { return x - y; });
    return s[Math.min(s.length - 1, Math.max(0, Math.floor(p * (s.length - 1))))];
  }
  function r2(v) { return Math.round(v * 100) / 100; }
  function hfovOf(mp) {
    if (mp.hfov) return +mp.hfov;
    return 2 * Math.atan(Math.tan((+mp.vfov || 75) * D / 2) * mp.w / mp.h) / D;
  }

  /* One frame's maps to a scene: per direction across his view (BINS of them, + to his left as
     everywhere in the page) how far the nearest thing at his height is, or how far he can see
     there is nothing; the openings among them; and the labelled things in view with their
     bearing and distance. */
  function reduce(mp) {
    var w = mp.w | 0, h = mp.h | 0, dep = mp.depth, lab = mp.labels, names = mp.classes || VOCAB;
    var vfov = +mp.vfov || 75, hfov = hfovOf(mp), p = (+mp.pitch || 0) * D, camH = mp.camH === undefined ? 0.1 : +mp.camH;
    var fy = (h / 2) / Math.tan(vfov * D / 2), fx = (w / 2) / Math.tan(hfov * D / 2);
    var cp = Math.cos(p), sp = Math.sin(p), half = hfov / 2, bw = hfov / BINS;
    var bins = [], things = {}, n = 0;
    for (var b = 0; b < BINS; b++) bins.push({ d: [], l: [] });
    for (var r = 0; r < h; r++) {
      var vy = (h / 2 - (r + 0.5)) / fy;
      for (var c = 0; c < w; c++) {
        var i = r * w + c, z = +dep[i];
        if (!(z > SELF_M) || !isFinite(z)) continue;
        var name = names[lab ? lab[i] : 0] || "unknown";
        if (name === "self" || name === "ceiling") continue;
        n++;
        var yc = vy * z, xc = ((w / 2 - (c + 0.5)) / fx) * z;
        var fwd = z * cp - yc * sp, up = camH + z * sp + yc * cp;
        if (fwd <= 0.01) continue;
        var dist = Math.hypot(fwd, xc), deg = Math.atan2(xc, fwd) / D;
        if (!PLAIN[name] && up < 1.6) {
          var t = things[name] || (things[name] = { n: 0, deg: [], d: [] });
          t.n++; t.deg.push(deg); t.d.push(dist);
        }
        if (name === "floor" || up > HMAX || up < HMIN) continue;
        var k = Math.floor((half - deg) / bw);
        if (k < 0 || k >= BINS) continue;
        bins[k].d.push(dist); bins[k].l.push(name);
      }
    }
    // How far the lowest ray carries before it rises past HMAX: an empty direction is open at
    // least this far, as far as this picture can tell.
    var low = p - vfov * D / 2, reach = low > 0.01 ? (HMAX - camH) / Math.tan(low) : 4;
    reach = Math.min(4, Math.max(0.3, reach));
    var cols = bins.map(function (bn, k) {
      var deg = half - bw * (k + 0.5), near = pct(bn.d, NEAR_PCT);
      if (near === null) return { deg: r2(deg), m: null, open: r2(reach), what: "" };
      var counts = {}, best = "", bc = 0;
      for (var j = 0; j < bn.d.length; j++) {
        if (bn.d[j] > near * 1.15 + 0.03) continue;
        var nm = bn.l[j]; counts[nm] = (counts[nm] || 0) + 1;
        if (counts[nm] > bc) { bc = counts[nm]; best = nm; }
      }
      return { deg: r2(deg), m: r2(near), what: best };
    });
    var list = Object.keys(things).map(function (k) {
      var t = things[k];
      return { what: k, deg: Math.round(pct(t.deg, 0.5)), m: r2(pct(t.d, 0.2)), share: n ? t.n / n : 0 };
    }).filter(function (t) { return t.share >= THING_SHARE; })
      .sort(function (a, b) { return a.m - b.m; }).slice(0, THINGS_MAX)
      .map(function (t) { return { what: t.what, deg: t.deg, m: t.m }; });
    // Ahead: the nearest thing in the middle directions, or, with none, how far they are clear.
    var mid = cols.filter(function (c) { return Math.abs(c.deg) <= 10; });
    var hit = mid.filter(function (c) { return c.m !== null; });
    var ahead = (hit.length ? hit.map(function (c) { return c.m; }) : mid.map(function (c) { return c.open; }))
      .reduce(function (a, v) { return a === null || v < a ? v : a; }, null);
    return { cols: cols, ahead: ahead, blocked: hit.length > 0,
      openings: openings(cols), things: list, reach: r2(reach), hfov: r2(hfov) };
  }

  /* Runs of directions much further off than what flanks them: a doorway, a gap between two
     pieces of furniture, the room going on past a corner. A run at the picture's edge counts
     when its one flank is near. */
  function openings(cols) {
    var far = function (c) { return c.m === null ? c.open : c.m; }, out = [], k = 0;
    while (k < cols.length) {
      var j = k, lo = far(cols[k]);
      while (j + 1 < cols.length && Math.abs(far(cols[j + 1]) - lo) < OPEN_GAIN_M) { j++; lo = Math.min(lo, far(cols[j])); }
      var left = k > 0 ? far(cols[k - 1]) : null, right = j < cols.length - 1 ? far(cols[j + 1]) : null;
      var flank = [left, right].filter(function (v) { return v !== null; });
      if (flank.length && flank.every(function (v) { return lo - v >= OPEN_GAIN_M; })) {
        out.push({ deg: Math.round((cols[k].deg + cols[j].deg) / 2), m: r2(lo), more: cols.slice(k, j + 1).some(function (c) { return c.m === null; }) });
      }
      k = j + 1;
    }
    return out;
  }

  /* From the newest scenes (oldest first, each {t, ahead, walking}), whether he is pushing
     against something rather than walking: walking forward all of the last PUSH_MS, with
     something close ahead, and no closer at the end than at the start. Or touching it. */
  function pushing(hist) {
    if (!hist.length) return false;
    var last = hist[hist.length - 1];
    if (!last.walking) return false;
    var prev = hist[hist.length - 2], touch = function (x) { return x.ahead !== null && x.ahead < TOUCH_M; };
    if (touch(last) && prev && prev.walking && touch(prev)) return true;
    var from = null;
    for (var i = hist.length - 1; i >= 0; i--) {
      if (!hist[i].walking || hist[i].ahead === null || hist[i].ahead > PUSH_NEAR_M) return false;
      if (last.t - hist[i].t >= PUSH_MS) { from = i; break; }
    }
    if (from === null) return false;
    var a = hist.slice(from, from + 3).map(function (x) { return x.ahead; });
    var b = hist.slice(-3).map(function (x) { return x.ahead; });
    return pct(a, 0.5) - pct(b, 0.5) < PUSH_GAIN_M;
  }

  function side(deg) {
    var a = Math.abs(deg);
    return a < 8 ? "ahead" : Math.round(a) + (deg > 0 ? "L" : "R");
  }
  function far(c) { return c.m === null ? c.open + " m or more, clear" : c.m + " m, " + (c.what || "something"); }
  /* One line for his next thought. Three directions (left, ahead, right of his picture), the
     openings and the things, in the words his map takes (degrees + to his left). */
  function line(sc) {
    if (!sc) return "";
    var cols = sc.cols, n = cols.length, parts = [];
    var near = function (from, to) {
      var best = null;
      for (var k = from; k < to; k++) { var c = cols[k], v = c.m === null ? c.open + 9 : c.m; if (!best || v < best.v) best = { v: v, c: c }; }
      return best && best.c;
    };
    var L = near(0, Math.floor(n / 3)), A = near(Math.floor(n / 3), n - Math.floor(n / 3)), R = near(n - Math.floor(n / 3), n);
    parts.push("left (" + side(L.deg) + "): " + far(L) + "; ahead: " + far(A) + "; right (" + side(R.deg) + "): " + far(R));
    if (sc.openings.length) parts.push("open way" + (sc.openings.length > 1 ? "s" : "") + ": " + sc.openings.map(function (o) { return side(o.deg) + ", " + o.m + " m" + (o.more ? " or more" : "") + " deep"; }).join("; "));
    if (sc.things.length) parts.push("in view: " + sc.things.map(function (t) { return t.what + " " + t.m + " m " + side(t.deg); }).join("; "));
    return "[your depth sense, from your camera just now (about " + Math.round(sc.hfov) + " degrees across, at your height): "
      + parts.join(". ") + ". Distances are across the floor. Names are its guess: you name what you see. "
      + "map:{saw:\"name\",deg:N} takes its distance when you give none.]";
  }

  /* The nearest thing at his height in one direction of a scene, or null where the scene is
     clear or does not reach. */
  // His words for the vocabulary's things, for matching a name he gives to what the scene saw.
  // Matched against the last word of his name for it, its head noun: "the bedside table" is a
  // table, not a bed, and "ottoman" is no man.
  var SAYS = { sofa: /^(couch|sofa|settee)$/, shelf: /^(shelf|shelves|bookshelf|bookcase)$/,
    animal: /^(cat|dog|animal|pet|kitten|puppy)$/, fridge: /^(fridge|refrigerator)$/,
    screen: /^(tv|television|screen|monitor)$/, counter: /^(counter|worktop)$/,
    person: /^(person|man|woman|dad|mum|mom|kid|child|people)$/ };
  function says(what, name) {
    var head = String(name || "").toLowerCase().trim().split(/\s+/).pop();
    if (!head) return false;
    // Plurals: "couches", "tables", "shelves" (as shelf).
    var forms = [head, head.replace(/s$/, ""), head.replace(/es$/, ""), head.replace(/ves$/, "f")];
    return forms.some(function (f) { return SAYS[what] ? SAYS[what].test(f) : f === what; });
  }
  function distAt(sc, deg, name) {
    if (!sc) return null;
    // The thing he named, when the scene saw one like it that way: not what stands in front.
    var named = sc.things.filter(function (x) { return Math.abs(x.deg - deg) <= 8 && says(x.what, name); })[0];
    if (named) return named.m;
    var best = null;
    sc.cols.forEach(function (c) {
      if (Math.abs(c.deg - deg) <= sc.hfov / sc.cols.length / 2 + 0.01 && c.m !== null) best = best === null ? c.m : Math.min(best, c.m);
    });
    if (best !== null) return best;
    var t = sc.things.filter(function (x) { return Math.abs(x.deg - deg) <= 6; })[0];
    return t ? t.m : null;
  }

  var api = { VOCAB: VOCAB, reduce: reduce, openings: openings, pushing: pushing, line: line, distAt: distAt,
    FRESH_MS: FRESH_MS, PUSH_MS: PUSH_MS, HMAX: HMAX };

  if (typeof window !== "undefined" && typeof document !== "undefined") {
    var latest = null, hist = [], fed = 0;
    var heading = function () {
      try { var m = window.gbMap && window.gbMap.map && window.gbMap.map(); return m && m.here && isFinite(+m.here.h) ? +m.here.h : null; } catch (e) { return null; }
    };
    var walkingFwd = function () {
      try { var M = typeof _motion !== "undefined" ? _motion : null, v = M && M.report && M.report.mv; return !!M && !M.loading && M.kind === "walk" && M.own !== "back" && !(v && (v.kind === "spin" || v.direction === "turn" || v.direction === "screen-backward")); } catch (e) { return false; }
    };
    /* A source's frame. Depth may come as millimetres (depth_mm) to keep the sim's messages
       small. Reduced at once; only the scene is kept. */
    api.feed = function (mp) {
      try {
        if (!mp || !(mp.w > 0) || !(mp.h > 0)) return null;
        if (!mp.depth && mp.depth_mm) mp.depth = mp.depth_mm.map(function (v) { return v / 1000; });
        var sc = reduce(mp), now = Date.now();
        // The page's own clock: a source's may run ahead of it, and a scene would never go stale.
        sc.t = now; sc.h = heading(); sc.src = mp.src || "";
        latest = sc; fed++;
        hist.push({ t: now, ahead: sc.blocked ? sc.ahead : null, walking: walkingFwd() });
        hist = hist.filter(function (x) { return now - x.t < api.PUSH_MS + 4000; });
        if (fed % 20 === 1) { try { logEv("vision", { src: sc.src, ahead: sc.ahead, open: sc.openings.length, things: sc.things.map(function (t) { return t.what; }).join(",") }); } catch (e) {} }
        return sc;
      } catch (e) { return null; }
    };
    api.fresh = function () { return latest && Date.now() - latest.t < FRESH_MS ? latest : null; };
    api.latest = function () { return latest; };
    api.isPushing = function () { return !!api.fresh() && pushing(hist); };
    api.clearHist = function () { hist = []; };
    // Only on legs: a phone on a desk stand or a dashboard has no use for where the floor goes.
    api.take = function () {
      try { if (typeof MODE !== "undefined" && MODE !== "legs") return ""; } catch (e) {}
      return line(api.fresh());
    };
    /* What is right ahead now, for a trip's next step: only from a scene taken facing within
       10 degrees of where he faces, so not one from halfway through the turn that led here. */
    api.aheadNow = function () {
      var sc = api.fresh(), h = heading();
      if (!sc || !sc.blocked || sc.h === null || h === null) return null;
      return Math.abs((h - sc.h + 540) % 360 - 180) <= 10 ? sc.ahead : null;
    };
    /* For his map: how far the thing he named at deg (from where he faces now) was, from the
       newest scene taken facing near there in the last 10 s. */
    api.distNow = function (deg, name) {
      try {
        if (!latest || Date.now() - latest.t > 10000) return null;
        var h = heading(), rel = latest.h === null || h === null ? deg : deg + ((h - latest.h + 540) % 360 - 180);
        return distAt(latest, rel, name);
      } catch (e) { return null; }
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.gbVision = api;
})(typeof window !== "undefined" ? window : this);
