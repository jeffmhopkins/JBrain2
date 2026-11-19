/* Every look Dave takes, kept in his context until he dreams (owner, 2026-11-19: "We could keep
   it in context for all of his looks until he dreamed"). Before this a thought carried only the
   newest camera picture, and a picture from one thought was gone by the next: what he saw
   lived on only as the words he said about it.

   Each picture a thought sends is kept as a small copy (LOOK_PX on its long side) tagged with
   when he took it and where his map had him standing and facing. The history replays each look
   inside the turn it was taken in, in time order, so the history ahead of a new turn reads the
   same from turn to turn and the box resumes from it: a thought pays only for its own newest
   looks, not for the pile. Looks taken since the last turn he kept (a watch tick, a quiet beat)
   ride one message at the end of the history until a kept turn takes them in. Looks whose turn
   the history window has dropped ride one message ahead of the talk, which only grows at its
   end, so a window that jumps forward costs the box only the looks that moved.

   A dream clears them: the pile holds only looks taken after the last dream. LOOK_MAX bounds a
   long day; past it the older half goes in one step, so the cache breaks once rather than every
   turn (the history window's own rule, gbHistWindow). It starts small: at the box's picture floor
   a small look reads as ~270 tokens, the same as a full frame, and a cold read of the whole pile
   (a halving, an engine restart) has to finish inside the turn's 90 s fuse, so it is raised only
   once that read is measured on the box. A thought with no person in it (a tick) keeps its look
   only if TICK_GAP_MS have passed since the last one kept or he has moved, so a carried or
   walking robot's watch ticks do not fill the pile in minutes. Fine detail still needs a fresh
   look: a small copy keeps the room, not the print on a box.

   The pile is kept in IndexedDB as well, so a reload or an update does not forget the day.

   The pure half (which turn each look rides in, the cap, the tag) is tested in node; the page
   half reads the creature page's globals (_live, camFacing) lazily. */
(function (root) {
  "use strict";
  var LOOK_PX = 160, LOOK_Q = 0.7, LOOK_MAX = 60, DB = "gb-looks", STORE = "looks";
  var TICK_GAP_MS = 30000, MOVED_M = 0.5, TURNED_DEG = 30;
  // Another persona's looks are left alone (it may be running in another tab), until this old.
  var STALE_MS = 3 * 86400000;
  /* A look that shows what the last one did is not kept: standing still, a person's turns
     send the same view over and over. "The same" is the grey 8x8 copies differing by under
     SAME_GREY a pixel, from within SAME_M and SAME_DEG of where the last was taken. */
  var SIG = 8, SAME_GREY = 6, SAME_M = 0.2, SAME_DEG = 15;

  /* Which history message each look rides in. `ts` is the time of each turn the history
     replays, oldest first. A look rides in the turn that sent it: the first turn whose reply
     landed after the thought that sent it began (`at`; its picture may be older than the last
     reply, and must not join that turn's message, which the box has already read). One newer
     than every turn is pending. One sent by `before` (the last turn the window has dropped) is
     early: its turn is gone from the history, so it rides one message ahead of it that only
     ever grows at its end. */
  function split(ts, looks, before) {
    var by = ts.map(function () { return []; }), pending = [], early = [];
    looks.forEach(function (l) {
      var at = isFinite(+l.at) ? +l.at : l.t;
      if (before && at <= before) { early.push(l); return; }
      for (var i = 0; i < ts.length; i++) if (+ts[i] > at) { by[i].push(l); return; }
      pending.push(l);
    });
    return { early: early, by: by, pending: pending };
  }

  /* The early looks' message. When the window drops turns their looks join its end, so what the
     box read of it before still reads the same and only the newcomers are read. */
  var EARLY = "(what you saw earlier since you last slept, before the talk below)";
  function early(looks) { return [{ type: "text", text: EARLY }].concat(parts(looks)); }

  /* The looks a thought may see: taken after the last dream, oldest first. */
  function since(looks, dreamT) {
    return looks.filter(function (l) { return l.t > dreamT; }).sort(function (a, b) { return a.t - b.t; });
  }

  /* Past `max`, keep the newer half: one break of the cached history, not one every turn. */
  function trim(looks, max) {
    max = max || LOOK_MAX;
    if (looks.length <= max) return looks;
    return looks.slice(looks.length - Math.floor(max / 2));
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function clock(t) {
    var d = new Date(t), h = d.getHours();
    return (h % 12 || 12) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) + (h < 12 ? " am" : " pm");
  }

  /* The words beside a look's picture. Opens "[camera" so a lane that cannot see drops it with
     the picture (callModel's blind strip). */
  function posed(p) { return !!p && isFinite(+p.x) && isFinite(+p.y) && isFinite(+p.h); }
  function tag(l) {
    var s = "[camera, a look you took at " + clock(l.t) + " with your " + (l.eye === "b" ? "rear eye" : "forward eye");
    if (posed(l.p)) {
      s += ", standing at (" + (+l.p.x).toFixed(1) + ", " + (+l.p.y).toFixed(1) + ") m on your map facing " + Math.round(+l.p.h) + " degrees";
      if (typeof l.p.room === "string" && l.p.room) s += " in the " + l.p.room;
    }
    return s + "]";
  }

  function wrap(a) { a = ((a + 180) % 360 + 360) % 360 - 180; return a === -180 ? 180 : a; }

  /* Whether look b shows what look a did: near-identical grey signatures (`g`, SIG x SIG
     values 0-255) from about the same spot and heading. Without a pose, the picture decides. */
  function same(a, b) {
    if (!a || !b || !a.g || !b.g || a.g.length !== b.g.length) return false;
    if (a.p && b.p && (Math.hypot(a.p.x - b.p.x, a.p.y - b.p.y) > SAME_M || Math.abs(wrap(a.p.h - b.p.h)) > SAME_DEG)) return false;
    var d = 0;
    for (var i = 0; i < a.g.length; i++) d += Math.abs(a.g[i] - b.g[i]);
    return d / a.g.length < SAME_GREY;
  }

  /* Whether a tick's look is worth keeping after `last`: enough time, or he has moved. */
  function tickKeeps(last, l) {
    if (!last || l.t - last.t >= TICK_GAP_MS) return true;
    if (!posed(last.p) || !posed(l.p)) return false;
    return Math.hypot(l.p.x - last.p.x, l.p.y - last.p.y) >= MOVED_M || Math.abs(wrap(l.p.h - last.p.h)) >= TURNED_DEG;
  }

  function parts(looks) {
    var out = [];
    looks.forEach(function (l) {
      out.push({ type: "text", text: tag(l) });
      out.push({ type: "image_url", image_url: { url: "data:image/jpeg;base64," + l.j } });
    });
    return out;
  }

  /* A replayed turn's message with its looks ahead of its words, as the live turn had its
     camera ahead of its words. */
  function content(looks, text) {
    return looks.length ? parts(looks).concat([{ type: "text", text: text }]) : text;
  }

  var api = {
    split: split, since: since, trim: trim, tag: tag, parts: parts, content: content, same: same,
    early: early, tickKeeps: tickKeeps,
    LOOK_PX: LOOK_PX, LOOK_MAX: LOOK_MAX,
  };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }

  var pile = [], loaded = false, db = null;

  function soul() {
    var s = root.__gbBoxSoul;
    return (s && typeof s.persona === "string" && s.persona) || "";
  }
  function dreamT() {
    try {
      var ld = _live && _live.scratch && _live.scratch.lastDream, t = (ld && typeof ld === "object" && +ld.t) || 0;
      return t > Date.now() ? 0 : t;
    } catch (e) { return 0; }
  }
  function pose() {
    try {
      var m = root.gbMap && root.gbMap.map && root.gbMap.map(), h = m && m.here;
      if (!h || !isFinite(+h.x) || !isFinite(+h.y) || !isFinite(+h.h)) return null;
      return { x: +h.x, y: +h.y, h: +h.h, room: typeof h.room === "string" ? h.room.slice(0, 40) : "" };
    } catch (e) { return null; }
  }

  function open(then) {
    if (db) { then(db); return; }
    try {
      var rq = root.indexedDB.open(DB, 1);
      rq.onupgradeneeded = function () { rq.result.createObjectStore(STORE, { keyPath: "id" }); };
      rq.onsuccess = function () { db = rq.result; then(db); };
      rq.onerror = function () {};
    } catch (e) {}
  }
  function tx(fn) {
    open(function (d) { try { fn(d.transaction(STORE, "readwrite").objectStore(STORE)); } catch (e) {} });
  }
  function drop(gone) { if (gone.length) tx(function (s) { gone.forEach(function (l) { s.delete(l.id); }); }); }

  /* The kept pile, read once: this persona's looks since its last dream. Older ones and other
     personas' are deleted as they are found. */
  function load() {
    tx(function (s) {
      var rq = s.getAll();
      rq.onsuccess = function () {
        var me = soul(), dt = dreamT(), keep = [], gone = [];
        (rq.result || []).forEach(function (l) {
          if (!l || typeof l.j !== "string" || !isFinite(+l.t)) { if (l && l.id !== undefined) gone.push(l); return; }
          if (l.soul !== me) { if (Date.now() - l.t > STALE_MS) gone.push(l); return; }
          if (l.t > dt) keep.push(l); else gone.push(l);
        });
        drop(gone);
        var have = {};
        pile.forEach(function (l) { have[l.id] = 1; });
        pile = trim(since(keep.filter(function (l) { return !have[l.id]; }).concat(pile), 0));
        loaded = true;
      };
    });
  }

  function shrink(j, then) {
    var img = new Image();
    img.onload = function () {
      try {
        var k = Math.min(1, LOOK_PX / Math.max(img.width, img.height)), c = document.createElement("canvas");
        c.width = Math.max(1, Math.round(img.width * k)); c.height = Math.max(1, Math.round(img.height * k));
        c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
        var j2 = c.toDataURL("image/jpeg", LOOK_Q).split(",")[1] || "";
        var g = document.createElement("canvas"), gx, px, sig = [];
        g.width = SIG; g.height = SIG; gx = g.getContext("2d");
        gx.drawImage(c, 0, 0, SIG, SIG);
        px = gx.getImageData(0, 0, SIG, SIG).data;
        for (var i = 0; i < px.length; i += 4) sig.push(Math.round(0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2]));
        then(j2, sig);
      } catch (e) {}
    };
    img.src = "data:image/jpeg;base64," + j;
  }

  root.gbLooks = {
    /* From think(), with the picture this thought sends (its time and base64 jpeg), the eye,
       when the thought began, and whether it was a tick. */
    add: function (f, eye, at, tick) {
      if (!f || typeof f.j !== "string" || !f.j || !isFinite(+f.t)) return;
      var me = soul(), t = +f.t, p = pose();
      at = isFinite(+at) ? +at : t;
      if (pile.some(function (l) { return l.t === t; })) return;
      shrink(f.j, function (j, g) {
        if (!j || soul() !== me) return;
        var l = { id: me + ":" + t, soul: me, t: t, at: at, j: j, g: g, eye: eye === "b" ? "b" : "f", p: p };
        if (same(pile[pile.length - 1], l)) return;
        if (tick && !tickKeeps(pile[pile.length - 1], l)) return;
        var before = pile;
        pile = trim(since(pile.concat([l]), dreamT()));
        drop(before.filter(function (x) { return pile.indexOf(x) < 0; }));
        tx(function (s) { s.put(l); });
        try { logEv("look_kept", { n: pile.length, kb: Math.round(j.length / 1024) }); } catch (e) {}
      });
    },
    /* The looks the history replays now: since the last dream, oldest first. A dream that
       landed since the last read deletes what it cleared. */
    now: function () {
      var dt = dreamT(), kept = since(pile, dt);
      if (kept.length !== pile.length) { drop(pile.filter(function (l) { return l.t <= dt; })); pile = kept; }
      return kept;
    },
    /* A key that changes when the pile does, for the warm-up and the seal. */
    key: function () { var l = pile[pile.length - 1]; return pile.length + ":" + (l ? l.t : 0) + (loaded ? "" : "~"); },
    split: split, content: content, parts: parts, early: early,
  };
  load();
})(typeof globalThis !== "undefined" ? globalThis : this);
