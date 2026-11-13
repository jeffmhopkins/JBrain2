/* GrowBot on the box: the conversation stays on screen (owner, 2026-11-13: "sometimes I
   interrupt it while it's replying and I can't see what it was saying anymore").

   Upstream shows one caption (#say) and, for 7 s, the person's last words (#youSaid). A new
   reply overwrites the caption, so a reply the person talked over is gone. This keeps every
   line on both sides, and for each of the creature's lines which parts were heard:

   - a fading trail of the last few lines above the caption, older ones dimmer; tapping a
     line opens it in place with the part the person never heard dimmed, behind a marker;
   - a log drawer (the trail's "log" pill, or a swipe up on the trail) with the whole day,
     a search, and "hear the rest" on any line that was cut short; it keeps a week.

   What was heard comes from upstream's own sentence queue. A streamed reply is voiced one
   sentence at a time through _chainQ; each queued sentence carries the caption offset it ends
   at (onSayStream's `upto`), each one _chainNext starts is playing, and the next _chainNext
   means it finished. Every path that drops the queue reports it first, and that marks whatever
   was playing or still queued as cut: silence() (barge-in, a new reply over the old one,
   pause), _ctxHush, and speakNow when a whole line takes the voice over a stream. A sentence cut
   while playing counts as unheard, since the person may have missed any of it. Text the page
   held back (_yieldNow: the person was talking, so the caption streamed silently) counts as
   unheard too. A muted creature is caption-only by choice, so nothing is dimmed there.

   The lines live on the phone only (localStorage, per persona, a week, 300 lines), the same
   place upstream keeps its own diary; they are never sent anywhere. Loaded in <head> before
   any page script; the page calls in through window.gbTalk, and nothing here may throw into
   the page. ?talklog=off turns it off. */
(function () {
  "use strict";

  var KEEP = 300;
  // A line longer than any reply, so a runaway caption cannot eat the origin's storage quota.
  var MAX_CHARS = 4000;
  var KEEP_MS = 7 * 24 * 3600 * 1000;
  var TRAIL_LINES = 3;
  // The trail is for "what did it just say", not the history: the drawer holds that.
  var TRAIL_MS = 4 * 60 * 1000;
  var SWIPE_PX = 40;

  var off = false;
  try { off = /[?&]talklog=off\b/.test(location.search); } catch (e) {}

  var log = [];
  var reply = null;
  var playing = null;
  var solo = null;
  var expanded = null;
  var nextId = 1;
  var store = null;
  var saveTimer = 0;
  var paintQueued = false;

  function now() { return Date.now(); }

  /* The persona this tab loaded, fixed once: another tab can move the phone's shared meta to a
     new persona while this one runs on, and this tab's lines must never land in that one's log
     (growbot-box.js keeps its soul writes to its own persona the same way). */
  var KEY = (function () {
    var persona = "";
    try {
      var b = window.__gbBoxSoul;
      persona = b && typeof b.persona === "string" ? b.persona
        : (JSON.parse(window.localStorage.getItem("gb_box_soul") || "{}") || {}).persona || "";
    } catch (e) {}
    return "gb_talk_log:" + persona;
  })();
  function storeKey() { return KEY; }

  function load() {
    try {
      store = window.localStorage;
      var raw = JSON.parse(store.getItem(storeKey()) || "[]");
      var cutoff = now() - KEEP_MS;
      log = (Array.isArray(raw) ? raw : []).filter(function (e) {
        return e && typeof e.text === "string" && typeof e.t === "number" && e.t > cutoff && (e.who === "me" || e.who === "you");
      }).slice(-KEEP);
      log.forEach(function (e) {
        e.id = nextId++;
        if (!Array.isArray(e.chunks)) e.chunks = [];
        // A reload ended the voice: whatever was still playing or queued was never heard.
        e.chunks.forEach(function (c) { if (c.st === "p" || c.st === "q") { c.st = "x"; e.cut = true; } });
      });
    } catch (e) { log = []; }
  }

  function save() {
    if (!store) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      try {
        store.setItem(storeKey(), JSON.stringify(log.slice(-KEEP).map(function (e) {
          return { who: e.who, text: e.text, t: e.t, cut: !!e.cut, held: !!e.held, soloCut: !!e.soloCut,
            chunks: e.chunks.map(function (c) { return { s: c.s, e: c.e, st: c.st }; }) };
        })));
      } catch (e) {}
    }, 800);
  }

  function add(who, text) {
    var e = { id: nextId++, who: who, text: text.slice(0, MAX_CHARS), t: now(), chunks: [], cut: false, held: false, soloCut: false };
    log.push(e);
    if (log.length > KEEP) log.splice(0, log.length - KEEP);
    return e;
  }

  function changed() { save(); paint(); }

  /* ---------- what the page tells us ---------- */

  // The person's words, typed or transcribed, as they enter upstream's one input funnel.
  function you(text) {
    var t = String(text || "").replace(/\s+/g, " ").trim();
    if (!t) return;
    add("you", t);
    changed();
  }

  // A think starts: the next caption that is not a stray system line is its reply.
  function begin() { reply = { entry: null, chunks: [] }; }

  function queued(it) {
    var upto = api.upto;
    api.upto = -1;
    if (!it || typeof upto !== "number" || upto < 0) return;
    if (!reply) begin();
    var last = reply.chunks[reply.chunks.length - 1];
    var c = { s: last ? last.e : 0, e: upto, st: "q" };
    reply.chunks.push(c);
    it.gbTalk = { c: c, r: reply };
  }

  // _chainNext runs when the previous sentence finished (or on the first one).
  function next() {
    var done = playing && playing.st === "p";
    if (done) playing.st = "ok";
    playing = null;
    if (done) save();
  }

  function start(it) {
    if (it && it.gbTalk) { playing = it.gbTalk.c; playing.st = "p"; }
  }

  // A muted creature is caption-only by choice: nothing it showed was missed.
  function held() {
    if (window.MUTED) return;
    if (reply) reply.held = true;
    if (reply && reply.entry) reply.entry.held = true;
  }

  // speakNow voices a whole line at once: the caption for it was written in the same tick.
  function soloLine(text) {
    // The caller wrote #say in this same tick, before the observer has seen it.
    try { var say = document.getElementById("say"); if (say) caption(say.textContent); } catch (e) {}
    var me = lastMe();
    var t = String(text || "").trim();
    solo = me && t && me.text.indexOf(t.slice(0, 24)) === 0 ? me : null;
  }

  function cut(chainQ, voiceActive) {
    var hit = false;
    if (playing && playing.st === "p") { playing.st = "x"; hit = true; markCut(playing); }
    playing = null;
    (chainQ || []).forEach(function (it) {
      if (it && it.gbTalk && it.gbTalk.c.st === "q") { it.gbTalk.c.st = "x"; hit = true; markCut(it.gbTalk.c); }
    });
    if (!hit && voiceActive && solo && solo === lastMe() && !solo.chunks.length) {
      solo.cut = true; solo.soloCut = true; hit = true;
    }
    solo = null;
    if (hit) changed();
  }

  function markCut(c) {
    log.forEach(function (e) { if (e.chunks.indexOf(c) >= 0) e.cut = true; });
    if (reply && reply.chunks.indexOf(c) >= 0) reply.cut = true;
  }

  function lastMe() {
    for (var i = log.length - 1; i >= 0; i--) if (log[i].who === "me") return log[i];
    return null;
  }

  /* ---------- the caption, watched ---------- */

  /* The settled caption (upstream's final `said`) differs from the streamed one: trimmed, a
     dash turned into a comma, real newlines where the stream had spaces, and on a reply cut by
     the token limit, shortened to its last whole sentence. */
  function norm(s) { return s.replace(/\s*—\s*/g, ", ").replace(/\s+/g, " ").trim(); }

  // The same reply: grown (streaming, or a move added after it), or settled shorter. A short
  // stray line is never taken as a shortened reply.
  function related(a, b) {
    var na = norm(a), nb = norm(b);
    if (!na.length) return false;
    return nb.indexOf(na) === 0 || (nb.length >= 24 && na.indexOf(nb) === 0);
  }

  // Chunk offsets were measured on the streamed text; carry them over to its settled form.
  function settle(e, t) {
    var old = e.text;
    var dash = (old.match(/—/g) || []).length > (t.match(/—/g) || []).length;
    var lead = /^\s/.test(t) ? 0 : old.length - old.replace(/^\s+/, "").length;
    function at(i) {
      var p = old.slice(0, i);
      if (dash) p = p.replace(/\s*—\s*/g, ", ");
      return Math.max(0, p.length - lead);
    }
    if (dash || lead) e.chunks.forEach(function (c) { c.s = at(c.s); c.e = at(c.e); });
    e.text = t.slice(0, MAX_CHARS);
  }

  function caption(text) {
    var t = String(text || "").replace(/\s+$/, "");
    if (!t.trim() || /^(…|\.\.\.)$/.test(t.trim())) return;
    if (reply && reply.entry && related(reply.entry.text, t)) {
      if (reply.entry.text !== t) { settle(reply.entry, t); changed(); }
      return;
    }
    if (reply && !reply.entry && (reply.chunks.length || !isRecent(t))) {
      var e = add("me", t);
      e.chunks = reply.chunks;
      e.held = !!reply.held;
      e.cut = !!reply.cut;
      reply.entry = e;
      changed();
      return;
    }
    // A line outside any reply (a dream's wake line, an out-of-energy line), or one the page
    // writes back after a pause: kept once.
    if (isRecent(t)) return;
    add("me", t);
    changed();
  }

  function isRecent(t) {
    for (var i = log.length - 1, n = 0; i >= 0 && n < 4; i--, n++) if (log[i].who === "me" && log[i].text === t) return true;
    return false;
  }

  /* ---------- what was heard ---------- */

  // The line as runs of heard and unheard text, and where the unheard part starts.
  function pieces(e) {
    var text = e.text, out = [], at = 0, gone = -1;
    function push(s, end, heard) {
      s = Math.min(s, text.length); end = Math.min(end, text.length);
      if (end <= s) return;
      if (!heard && gone < 0) gone = s;
      var last = out[out.length - 1];
      if (last && last.heard === heard) last.text += text.slice(s, end);
      else out.push({ text: text.slice(s, end), heard: heard });
    }
    if (e.soloCut && !e.chunks.length) { push(0, text.length, false); return { runs: out, gone: gone }; }
    e.chunks.forEach(function (c) { push(Math.max(at, c.s), c.e, c.st !== "x"); at = Math.max(at, c.e); });
    push(at, text.length, !(e.cut || e.held));
    return { runs: out, gone: gone };
  }

  function rest(e) {
    var p = pieces(e);
    return p.gone < 0 ? "" : e.text.slice(p.gone).trim();
  }

  function hear(e) {
    var r = rest(e);
    if (!r) return;
    try { if (typeof window.speak === "function") window.speak(r, "calm"); } catch (err) {}
  }

  /* ---------- drawing ---------- */

  var css = [
    "#gbTrail{display:flex;flex-direction:column;gap:9px;padding:0 18px 10px;pointer-events:none}",
    "body.editing #gbTrail,body.editing #gbLog{display:none!important}",
    "#gbTrail .gbtL{pointer-events:auto;cursor:pointer;font-size:.8em;line-height:1.4;",
    " color:#fff;text-shadow:2px 2px 0 #000,0 0 8px rgba(0,0,0,.8);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;transition:opacity .4s}",
    "#gbTrail .gbtL.you{align-self:flex-end;text-align:right;max-width:82%;color:#9fd;font-size:.62em}",
    "#gbTrail .gbtCut{color:#ff8a73;margin-right:6px}",
    "#gbTrail .gbtTop{display:flex;justify-content:flex-end}",
    "body:not(.editing) #liveCard #gbTrail button{pointer-events:auto;font-family:'Departure Mono',ui-monospace,monospace;font-size:11px;letter-spacing:.06em;",
    " text-transform:uppercase;min-height:30px;padding:4px 10px;border-radius:0;border:1px solid rgba(120,160,200,.3);background:rgba(14,22,34,.78);color:#E8F6FF;cursor:pointer}",
    "body:not(.editing) #liveCard #gbTrail button.hot{border-color:#ff4a2e;color:#ffb3a4}",
    "#gbTrail .gbtOpen{pointer-events:auto;background:rgba(26,11,9,.9);border:1px solid #6a2a20;padding:10px 12px;",
    " font-family:'Departure Mono',ui-monospace,monospace;font-size:14px;line-height:1.5;color:#f3e6e3;max-height:38vh;overflow:auto}",
    "#gbTrail .gbtOpen.you{border-color:#1d4d46;background:rgba(12,36,32,.9);color:#c9f6ee}",
    "#gbTrail .gbtActs,#gbLog .gbtActs{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}",
    ".gbtGone{color:#9a7c76}",
    ".gbtMark{display:inline-block;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:#ff8a73;border:1px solid rgba(255,138,115,.45);padding:0 5px;margin:0 4px;vertical-align:1px}",
    "#gbLog{position:fixed;left:0;right:0;bottom:0;height:58vh;z-index:60;display:none;flex-direction:column;background:#0a1019;",
    " border-top:1px solid rgba(120,160,200,.3);box-shadow:0 -16px 40px rgba(0,0,0,.7);font-family:'Departure Mono',ui-monospace,monospace;color:#E8F6FF;",
    " padding-bottom:env(safe-area-inset-bottom,0px)}",
    "#gbLog.on{display:flex}",
    "#gbLog .gblGrab{height:22px;flex:none;display:flex;justify-content:center;align-items:center;touch-action:none}",
    "#gbLog .gblGrab i{width:44px;height:4px;background:#3a4656}",
    "#gbLog .gblHead{display:flex;gap:8px;align-items:center;padding:0 12px 10px;border-bottom:1px solid rgba(120,160,200,.14);flex:none;touch-action:none}",
    "#gbLog .gblHead b{font-size:12px;letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}",
    "#gbLog input{flex:1;min-width:0;background:#0E1622;border:1px solid rgba(120,160,200,.22);color:#E8F6FF;font:16px 'Departure Mono',ui-monospace,monospace;padding:6px 8px;border-radius:0}",
    "#gbLog button{font-family:'Departure Mono',ui-monospace,monospace;font-size:11px;letter-spacing:.06em;text-transform:uppercase;min-height:32px;padding:4px 9px;",
    " border-radius:0;border:1px solid rgba(120,160,200,.3);background:#0E1622;color:#E8F6FF;cursor:pointer}",
    "#gbLog button.hot{border-color:#6a2a20;color:#ffb3a4}",
    "#gbLog .gblList{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:10px 12px 14px;display:flex;flex-direction:column;gap:10px;font-size:14px;line-height:1.5}",
    "#gbLog .gblDay{align-self:center;font-size:10px;color:#6d7f94;letter-spacing:.08em;text-transform:uppercase}",
    "#gbLog .gblM{max-width:86%;padding:8px 11px;border:1px solid;white-space:pre-wrap;word-wrap:break-word}",
    "#gbLog .gblM.me{align-self:flex-start;background:#1a0b09;border-color:#4a1a14;color:#f3e6e3}",
    "#gbLog .gblM.you{align-self:flex-end;background:#0c2420;border-color:#1d4d46;color:#c9f6ee}",
    "#gbLog .gblT{display:block;font-size:10px;color:#8a7470;margin-top:4px;letter-spacing:.04em}",
    "#gbLog .gblM.you .gblT{color:#5f978d;text-align:right}",
    "#gbLog .gblNone{align-self:center;color:#6d7f94;font-size:12px;margin-top:20px;text-align:center}"
  ].join("\n");

  var trailEl = null, logEl = null, listEl = null, findEl = null, titleEl = null;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function button(label, hot, fn) {
    var b = el("button", hot ? "hot" : "", label);
    b.type = "button";
    b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(); });
    return b;
  }

  function muted() { return !!window.MUTED; }

  // The whole line, its unheard part dimmed behind a marker.
  function fill(node, e) {
    var p = pieces(e);
    var marked = false;
    p.runs.forEach(function (r) {
      if (!r.heard && !marked) {
        marked = true;
        node.appendChild(el("span", "gbtMark", e.cut ? "✂ you cut in" : "not said aloud"));
      }
      node.appendChild(el("span", r.heard ? "" : "gbtGone", r.text));
    });
  }

  function hearButton(e) {
    return rest(e) && !muted() ? button("▶ hear the rest", true, function () { hear(e); }) : null;
  }

  function name() {
    try { var n = window._live && window._live.goals && window._live.goals.selfName; if (n) return String(n).slice(0, 24); } catch (e) {}
    return "";
  }

  // Which line the caption or the "you:" line is showing right now: the trail stops above it.
  function shownFrom() {
    var say = document.getElementById("say"), ys = document.getElementById("youSaid");
    var cap = say ? say.textContent.replace(/\s+$/, "") : "";
    var k = log.length;
    for (var i = log.length - 1; i >= 0; i--) {
      if (log[i].who === "me") { if (log[i].text === cap) k = i; break; }
    }
    if (ys && ys.style.display !== "none" && ys.textContent) {
      for (var j = log.length - 1; j >= 0; j--) if (log[j].who === "you") { k = Math.min(k, j); break; }
    }
    return k;
  }

  function paint() {
    if (paintQueued) return;
    paintQueued = true;
    var raf = window.requestAnimationFrame || function (f) { return setTimeout(f, 16); };
    raf(function () { paintQueued = false; try { paintTrail(); if (logEl && logEl.classList.contains("on")) paintLog(); } catch (e) {} });
  }

  // The trail speaks in the caption's own type, a size down, whatever the page set it to.
  function matchCaption() {
    var say = document.getElementById("say");
    if (!say || !window.getComputedStyle) return;
    var cs = window.getComputedStyle(say);
    trailEl.style.fontFamily = cs.fontFamily;
    trailEl.style.fontSize = cs.fontSize;
  }

  function paintTrail() {
    if (!trailEl) return;
    matchCaption();
    trailEl.textContent = "";
    var k = shownFrom(), from = Math.max(0, k - TRAIL_LINES), cutoff = now() - TRAIL_MS;
    var lines = log.slice(from, k).filter(function (e) { return e.t > cutoff || e === expanded; });
    if (expanded && log.slice(from, k).indexOf(expanded) < 0) expanded = null;
    if (log.length) {
      var top = el("div", "gbtTop");
      top.appendChild(button("≡ log", false, openLog));
      trailEl.appendChild(top);
    }
    lines.forEach(function (e, i) {
      if (e === expanded) {
        var box = el("div", "gbtOpen" + (e.who === "you" ? " you" : ""));
        fill(box, e);
        var acts = el("div", "gbtActs");
        var h = e.who === "me" ? hearButton(e) : null;
        if (h) acts.appendChild(h);
        acts.appendChild(button("close", false, function () { expanded = null; paint(); }));
        box.appendChild(acts);
        trailEl.appendChild(box);
        return;
      }
      var line = el("div", "gbtL" + (e.who === "you" ? " you" : ""));
      if (e.who === "me" && pieces(e).gone >= 0) line.appendChild(el("span", "gbtCut", "✂"));
      line.appendChild(document.createTextNode(e.text));
      line.style.opacity = String([0.4, 0.62, 0.85][TRAIL_LINES - lines.length + i] || 0.85);
      line.addEventListener("click", function (ev) { ev.stopPropagation(); expanded = e; paint(); });
      trailEl.appendChild(line);
    });
  }

  function dayLabel(t) {
    var d = new Date(t), today = new Date();
    var y = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "today";
    if (d.toDateString() === y.toDateString()) return "yesterday";
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }

  function clock(t) {
    var d = new Date(t);
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }

  function paintLog() {
    if (!listEl) return;
    var nearBottom = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 60;
    var q = (findEl && findEl.value || "").trim().toLowerCase();
    var n = name();
    titleEl.textContent = n ? "log · " + n : "log";
    listEl.textContent = "";
    var day = "", shown = 0;
    log.forEach(function (e) {
      if (q && e.text.toLowerCase().indexOf(q) < 0) return;
      var dl = dayLabel(e.t);
      if (dl !== day) { day = dl; listEl.appendChild(el("div", "gblDay", dl)); }
      var m = el("div", "gblM " + e.who);
      if (e.who === "me") fill(m, e); else m.appendChild(document.createTextNode(e.text));
      if (e.who === "me") {
        var h = hearButton(e);
        if (h) { var acts = el("div", "gbtActs"); acts.appendChild(h); m.appendChild(acts); }
      }
      m.appendChild(el("span", "gblT", clock(e.t) + (e.cut ? " · cut short" : "")));
      listEl.appendChild(m);
      shown++;
    });
    if (!shown) listEl.appendChild(el("div", "gblNone", q ? "nothing matches" : "nothing said yet"));
    if (nearBottom || listEl.dataset.fresh) { listEl.scrollTop = listEl.scrollHeight; delete listEl.dataset.fresh; }
  }

  function openLog() {
    if (!logEl) return;
    logEl.classList.add("on");
    listEl.dataset.fresh = "1";
    paintLog();
  }

  function closeLog() {
    if (!logEl) return;
    logEl.classList.remove("on");
    if (findEl) findEl.value = "";
  }

  function clearLog() {
    log = [];
    expanded = null;
    reply = reply ? { entry: null, chunks: [] } : null;
    try { if (store) store.removeItem(storeKey()); } catch (e) {}
    paint();
    paintLog();
  }

  function swipe(node, dir, fn) {
    var y0 = null;
    node.addEventListener("touchstart", function (ev) { y0 = ev.touches[0].clientY; }, { passive: true });
    node.addEventListener("touchend", function (ev) {
      if (y0 == null) return;
      var dy = ev.changedTouches[0].clientY - y0;
      y0 = null;
      if (dir * dy > SWIPE_PX) fn();
    }, { passive: true });
  }

  function build() {
    var style = el("style");
    style.id = "gbTalkCss";
    style.textContent = css;
    document.head.appendChild(style);

    trailEl = document.getElementById("gbTrail");
    if (trailEl) swipe(trailEl, -1, openLog);

    logEl = el("div");
    logEl.id = "gbLog";
    var grab = el("div", "gblGrab");
    grab.appendChild(el("i"));
    var head = el("div", "gblHead");
    titleEl = el("b", "", "log");
    findEl = el("input");
    findEl.type = "search";
    findEl.placeholder = "search";
    findEl.setAttribute("aria-label", "search the log");
    findEl.addEventListener("input", paintLog);
    head.appendChild(titleEl);
    head.appendChild(findEl);
    head.appendChild(button("clear", false, clearLog));
    head.appendChild(button("✕", false, closeLog));
    listEl = el("div", "gblList");
    logEl.appendChild(grab);
    logEl.appendChild(head);
    logEl.appendChild(listEl);
    swipe(grab, 1, closeLog);
    swipe(head, 1, closeLog);
    document.body.appendChild(logEl);

    var say = document.getElementById("say");
    if (say && window.MutationObserver) {
      new MutationObserver(function () { try { caption(say.textContent); } catch (e) {} })
        .observe(say, { childList: true, characterData: true, subtree: true });
    }
    var ys = document.getElementById("youSaid");
    if (ys && window.MutationObserver) {
      new MutationObserver(paint).observe(ys, { attributes: true, attributeFilter: ["style"] });
    }
    setInterval(paint, 15000);
    paint();
  }

  function guard(fn) {
    return function () { if (off) return; try { return fn.apply(null, arguments); } catch (e) {} };
  }

  var api = {
    upto: -1,
    you: guard(you),
    begin: guard(begin),
    queued: guard(queued),
    next: guard(next),
    start: guard(start),
    held: guard(held),
    solo: guard(soloLine),
    cut: guard(cut),
    caption: guard(caption),
    // For tests and the debug console: the lines as runs of heard and unheard text.
    lines: function () { return log.map(function (e) { return { who: e.who, text: e.text, cut: !!e.cut, runs: pieces(e).runs }; }); },
    open: guard(openLog)
  };
  window.gbTalk = api;

  if (off) return;
  load();
  function ready() { try { build(); } catch (e) {} }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
  else ready();
})();
