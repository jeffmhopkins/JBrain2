/* GrowBot on the box: every spoken word goes to the box's Kokoro voice (tts-stt), through the
   same-origin /api/fish-tts the shim answers. Upstream has two other voices: meSpeak running in
   the page, and the browser's speechSynthesis, which on Chrome can be a cloud voice. apply.py
   drops meSpeak and makes the Kokoro lane the default; this file replaces speechSynthesis so
   the fallback paths that remain also speak through the box and never through anything else.
   Loaded as the first script in <head>, before any page code can capture the real one. */
(function () {
  "use strict";

  var queue = [];
  var current = null;
  var abort = null;
  var watchdog = 0;
  // One element for every line: iOS lets an element play without a gesture only once that
  // same element has played inside one, so a fresh Audio() per line would be refused.
  var el = null;
  var unlocked = false;

  function element() {
    if (!el) {
      el = new Audio();
      el.preload = "auto";
    }
    return el;
  }

  function silentWav() {
    var rate = 8000, samples = 800, buf = new ArrayBuffer(44 + samples * 2), v = new DataView(buf);
    function str(o, s) { for (var i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); }
    str(0, "RIFF"); v.setUint32(4, 36 + samples * 2, true); str(8, "WAVEfmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true);
    v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, samples * 2, true);
    return new Blob([buf], { type: "audio/wav" });
  }

  function unlock() {
    if (unlocked || current) return;
    unlocked = true;
    var a = element(), url = URL.createObjectURL(silentWav());
    a.src = url;
    a.play().catch(function () { unlocked = false; }).then(function () { URL.revokeObjectURL(url); });
  }
  ["pointerdown", "touchend", "keydown"].forEach(function (type) {
    window.addEventListener(type, unlock, { capture: true, passive: true });
  });

  function emit(utterance, type) {
    // A real SpeechSynthesisUtterance is an EventTarget, and dispatching also runs its on<type>
    // property, so calling both would fire the handler twice.
    try {
      utterance.dispatchEvent(new Event(type));
    } catch (e) {
      var handler = utterance && utterance["on" + type];
      if (typeof handler === "function") handler.call(utterance, { type: type, utterance: utterance });
    }
  }

  function settle() {
    synth.speaking = !!current;
    synth.pending = queue.length > 0;
  }

  function release() {
    clearTimeout(watchdog);
    watchdog = 0;
    if (abort) {
      try { abort.abort(); } catch (e) {}
      abort = null;
    }
    if (el) {
      try {
        el.pause();
        if (el.src && el.src.indexOf("blob:") === 0) URL.revokeObjectURL(el.src);
        el.onended = el.onerror = el.onloadedmetadata = null;
      } catch (e) {}
    }
  }

  function finish(u, type) {
    if (current !== u) return;
    current = null;
    release();
    settle();
    emit(u, type);
    pump();
  }

  function pump() {
    if (current || !queue.length) return settle();
    var u = (current = queue.shift());
    settle();
    var text = String(u.text || "").trim();
    if (!text) {
      // The page primes speech with a blank line; nothing to say, and nothing to send.
      setTimeout(function () { finish(u, "end"); }, 0);
      return;
    }
    abort = typeof AbortController === "function" ? new AbortController() : null;
    // A box that never answers must not stall every line queued behind this one.
    watchdog = setTimeout(function () { finish(u, "error"); }, 15000 + text.length * 60);
    fetch("/api/fish-tts", {
      method: "POST",
      headers: { "content-type": "application/json", "x-fish-key": "box" },
      body: JSON.stringify({ text: text, voice: window.__gbBoxVoice || "", model: "kokoro", speed: u.rate || 1 }),
      signal: abort ? abort.signal : undefined,
    })
      .then(function (r) {
        if (r.status !== 200) throw r.status;
        return r.blob();
      })
      .then(function (blob) {
        if (current !== u) return;
        var a = element();
        a.onended = function () { finish(u, "end"); };
        a.onerror = function () { finish(u, "error"); };
        a.onloadedmetadata = function () {
          if (current !== u || !isFinite(a.duration)) return;
          clearTimeout(watchdog);
          watchdog = setTimeout(function () { finish(u, "error"); }, a.duration * 1000 + 2000);
        };
        a.src = URL.createObjectURL(blob);
        a.volume = typeof u.volume === "number" ? Math.max(0, Math.min(1, u.volume)) : 1;
        emit(u, "start");
        return a.play();
      })
      .catch(function () { finish(u, "error"); });
  }

  var synth = {
    speaking: false,
    pending: false,
    paused: false,
    onvoiceschanged: null,
    getVoices: function () { return []; },
    speak: function (utterance) {
      queue.push(utterance);
      pump();
    },
    cancel: function () {
      var dropped = queue;
      queue = [];
      if (current) finish(current, "end");
      settle();
      dropped.length = 0;
    },
    pause: function () {},
    resume: function () {},
    addEventListener: function () {},
    removeEventListener: function () {},
    dispatchEvent: function () { return true; },
  };

  try {
    Object.defineProperty(window, "speechSynthesis", { configurable: false, get: function () { return synth; } });
  } catch (e) {}
  window.__growbotBoxVoice = true;
})();

/* The installed app (Android's Install app): the manifest names /start, and this worker only
   answers when the box is out of reach. Registered here, not in the page, so no inline script
   (and no new CSP hash) is needed. */
(function () {
  "use strict";
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
  });
})();

/* A persona's soul lives on the box (plan §2c, wave G4b). The creature keeps its whole soul in
   one localStorage key, pb2_soul (soul.js), read once as the page boots. So before any page code
   runs, this pulls the box's copy into that key, and moves the page onto the robot the persona
   wears (or off any robot); while it runs, every write of the key is pushed back against the
   version it came from, and a poll notices an owner's edit, a hand-over or a deletion.
   gb_box_soul, shared by the phone's tabs, is {persona, version, dirty}: a write not yet on the
   box survives a closed page or a box that did not answer, and is sent later, only ever into
   the persona it came from (the box refuses a push that names another).
   A G2 session (no persona) gets 204 and the creature keeps its soul on the phone, as before;
   gb_phone_soul keeps that creature while the phone wears a persona, and gives it back. */
(function () {
  "use strict";
  if (location.pathname !== "/v3/next.html") return;
  var KEY = "pb2_soul", META = "gb_box_soul", OWN = "gb_phone_soul", PATH = "/api/box/persona";
  var POLL_MS = 10000, PUSH_AFTER_MS = 1500, IDLE_MS = 20000;
  var store;
  try { store = window.localStorage; store.getItem(KEY); } catch (e) { return; }
  var realSet = Storage.prototype.setItem;

  // Read fresh every time: another tab of this phone may have pushed since.
  function meta() {
    try {
      var m = JSON.parse(store.getItem(META) || "null");
      return m && typeof m === "object" ? m : {};
    } catch (e) { return {}; }
  }
  function setMeta(m) { try { realSet.call(store, META, JSON.stringify(m)); } catch (e) {} }
  function put(key, value) { try { realSet.call(store, key, value); return true; } catch (e) { return false; } }

  // Synchronous on purpose: pb2_soul must hold the box's soul before soul.js reads it.
  function sync(method, path, body) {
    var r = new XMLHttpRequest();
    try {
      r.open(method, path, false);
      if (body) r.setRequestHeader("content-type", "application/json");
      r.send(body || null);
    } catch (e) { return { status: 0, json: null }; }
    var json = null;
    try { json = JSON.parse(r.responseText); } catch (e) {}
    return { status: r.status, json: json };
  }

  function localSoul() {
    try { return JSON.parse(store.getItem(KEY) || "null"); } catch (e) { return null; }
  }

  function stopWith(title, text) {
    try { window.stop(); } catch (e) {}
    document.documentElement.innerHTML =
      '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
      "<title>GrowBot</title><style>" +
      "body{margin:0;background:#0b0b1a;color:#e6edf3;font:17px/1.5 system-ui,sans-serif}" +
      "main{max-width:480px;margin:0 auto;padding:48px 16px;display:grid;gap:16px}" +
      "h1{font-size:1.4rem;margin:0}p{margin:0;color:#b8c4cf}" +
      "a{display:block;text-align:center;padding:12px 16px;border-radius:10px;border:1px solid #7fd89a;" +
      "color:#7fd89a;font-weight:600;text-decoration:none}</style></head>" +
      "<body><main><h1></h1><p></p><a href=\"/start\">Try again</a></main></body>";
    document.querySelector("h1").textContent = title;
    document.querySelector("p").textContent = text;
  }

  function away(status) {
    // The faces it saved leave this phone with it.
    window.__gbBoxSoul = { away: true };
    if (status === 404) {
      stopWith("This creature isn't on the box any more",
        "It was deleted. Open another one from JBrain on your main phone: GrowBot, then Open.");
    } else {
      stopWith("This creature is open on another phone",
        "It lives on one phone at a time. To bring it back here, open it from JBrain on your " +
        "main phone: GrowBot, then Open.");
    }
  }

  // Only the robot is compared, so the page's own query (and how it spells it) never loops.
  function onRobot(wearing) {
    var q = new URLSearchParams(location.search);
    return (q.get("id") || "").toLowerCase() === wearing.toLowerCase() && q.get("mode") === "legs";
  }
  function moveTo(wearing) {
    var q = new URLSearchParams(location.search);
    if (wearing) q.set("id", wearing); else q.delete("id");
    q.set("mode", "legs");
    location.replace(location.pathname + "?" + q.toString() + location.hash);
  }

  function isAway(status, json) {
    return status === 404 || (status === 409 && json && json.error === "moved");
  }
  // The box will never take this write (not its persona's, too big, not a soul): let it go.
  function refused(status, json) {
    return status === 413 || status === 400 ||
      (status === 409 && json && (json.error === "newer" || json.error === "other"));
  }

  var state = sync("GET", PATH + "/state");
  // The face this persona wears, read by growbot-face.js, which loads next.
  if (state.status === 200 && state.json && typeof state.json.face === "string") {
    window.__gbBoxFace = state.json.face;
  }
  // The voice it speaks in ("" is the box's default), read at every line, so a poll's new one
  // is the next line's voice.
  if (state.status === 200 && state.json && typeof state.json.voice === "string") {
    window.__gbBoxVoice = state.json.voice;
  }
  window.addEventListener("gb-box-state", function (ev) {
    var v = ev && ev.detail && ev.detail.voice;
    if (typeof v === "string") window.__gbBoxVoice = v;
  });
  if (state.status === 204) {
    window.__gbBoxSoul = { g2: true };
    // A G2 session: the phone's own creature comes back if a persona had the key (or one
    // was set aside and the key since dropped).
    var own = null;
    try { own = store.getItem(OWN); } catch (e) {}
    if (own || meta().persona) {
      try { if (own) realSet.call(store, KEY, own); else store.removeItem(KEY); store.removeItem(OWN); } catch (e) {}
      setMeta({});
    }
    return;
  }
  if (isAway(state.status, state.json)) { away(state.status); return; }
  var persona = state.status === 200 && state.json ? state.json.persona : "";
  var m = meta();

  // 1. A write the box never got. Kept (and the page runs on it) until the box takes it or
  //    says it never will; dropped when it belongs to a persona this phone no longer holds.
  var keep = false;
  if (m.dirty && persona && m.persona === persona) {
    var pending = localSoul();
    if (pending) {
      var sent = sync("POST", PATH, JSON.stringify({ persona: m.persona, base: m.version, soul: pending }));
      if (isAway(sent.status, sent.json)) { away(sent.status); return; }
      if (sent.status === 200 && sent.json) setMeta({ persona: persona, version: sent.json.version, dirty: false });
      else keep = !refused(sent.status, sent.json);
    }
  } else if (m.dirty && !persona) {
    keep = true;   // the box did not answer: nothing is known yet, so nothing is thrown away
  }

  // 2. The box's soul, which every write this page makes starts from.
  var wearing = persona ? state.json.wearing || "" : null;
  if (!keep) {
    var pulled = sync("GET", PATH);
    if (isAway(pulled.status, pulled.json)) { away(pulled.status); return; }
    if (pulled.status === 200 && pulled.json && pulled.json.soul) {
      if (!meta().persona) {
        // The phone's own creature (a G2 session's, or none) is set aside, not lost, and
        // never replaced by whatever ran while the box was out of reach.
        var mine = null, kept = null;
        try { mine = store.getItem(KEY); kept = store.getItem(OWN); } catch (e) {}
        if (mine && !kept) put(OWN, mine);
      }
      if (!put(KEY, JSON.stringify(pulled.json.soul))) return;
      setMeta({ persona: pulled.json.persona, version: pulled.json.version, dirty: false });
      wearing = pulled.json.wearing || "";
    } else if (!meta().persona) {
      // Nothing from the box and nothing of a persona here: the creature runs as it is.
      return;
    }
  }

  // 3. On the robot the persona wears, or on none: the relay lets only the rider drive.
  if (wearing !== null) {
    if (!wearing) { try { store.removeItem("gb_walk_id"); } catch (e) {} }
    if (!onRobot(wearing)) { moveTo(wearing); return; }
  }

  // 4. Every write of the soul goes to the box, a moment after the last one, as the persona
  //    this tab loaded: a tab still open on a persona the phone has since swapped never
  //    pushes into the new one, whatever the shared meta now says.
  var loaded = meta().persona;
  // The visual memories below follow the persona this tab runs as.
  window.__gbBoxSoul = { persona: loaded };
  var gen = 0, timer = 0, inflight = false, lastInput = Date.now(), held = false;
  Storage.prototype.setItem = function (key) {
    var out = realSet.apply(this, arguments);
    if (this === store && key === KEY && !held) {
      gen++;
      var now = meta();
      now.dirty = true;
      setMeta(now);
      clearTimeout(timer);
      timer = setTimeout(push, PUSH_AFTER_MS);
    }
    return out;
  };

  function reloadWhenIdle() {
    if (document.hidden || Date.now() - lastInput > IDLE_MS) location.reload();
    else setTimeout(reloadWhenIdle, 5000);
  }

  function push() {
    if (held) return;
    if (inflight) { timer = setTimeout(push, PUSH_AFTER_MS); return; }
    var soul = localSoul(), at = meta();
    if (!soul || !loaded) return;
    if (at.persona !== loaded) { held = true; location.reload(); return; }
    var mine = gen;
    inflight = true;
    fetch(PATH, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ persona: loaded, base: at.version, soul: soul }),
    }).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (j) { return { status: r.status, json: j }; });
    }).then(function (r) {
      inflight = false;
      if (r.status === 200 && r.json && typeof r.json.version === "number") {
        var now = meta();
        now.version = r.json.version;
        if (mine === gen) now.dirty = false;
        setMeta(now);
        if (mine !== gen) timer = setTimeout(push, PUSH_AFTER_MS);
      } else if (r.status === 409 && r.json && r.json.error === "newer") {
        // The owner changed it from JBrain: their edit wins, and the page restarts on it.
        held = true;
        var after = meta();
        after.dirty = false;
        setMeta(after);
        reloadWhenIdle();
      } else if (r.status === 409 && r.json && r.json.error === "other") {
        // A persona this phone no longer holds: its soul is on the box, not this phone's to
        // keep or to set aside as the phone's own.
        held = true;
        setMeta({});
        try { store.removeItem(KEY); } catch (e) {}
        location.reload();
      } else if (isAway(r.status, r.json)) {
        held = true;
        location.reload();
      } else if (r.status !== 413 && r.status !== 400) {
        // A box that did not answer: try again (a soul the box refuses waits for the next write).
        timer = setTimeout(push, POLL_MS);
      }
    }).catch(function () {
      inflight = false;
      timer = setTimeout(push, POLL_MS);
    });
  }
  if (meta().dirty) timer = setTimeout(push, PUSH_AFTER_MS);

  // 5. An owner's edit, a hand-over or a deletion, noticed within a poll.
  function poll() {
    fetch(PATH + "/state").then(function (r) {
      return r.json().catch(function () { return null; }).then(function (j) { return { status: r.status, json: j }; });
    }).then(function (r) {
      if (isAway(r.status, r.json)) { held = true; location.reload(); return; }
      if (r.status !== 200 || !r.json) return;
      try { window.dispatchEvent(new CustomEvent("gb-box-state", { detail: r.json })); } catch (e) {}
      if (!onRobot(r.json.wearing || "")) { held = true; location.reload(); return; }
      var now = meta();
      if (r.json.version > (now.version || 0) && !now.dirty && !inflight) { held = true; reloadWhenIdle(); }
    }).catch(function () {});
  }
  setInterval(function () { if (!document.hidden) poll(); }, POLL_MS);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
    else if (meta().dirty && !inflight) push();
  });
  ["pointerdown", "keydown", "touchstart"].forEach(function (type) {
    window.addEventListener(type, function () { lastInput = Date.now(); }, { capture: true, passive: true });
  });
})();

/* A persona's settings live on the box too (migration 0263): the phone is only a display (owner,
   2026-11-12). Upstream keeps what a creature chose or was told to remember outside its soul,
   each in a localStorage key of its own (its coat colour, its games guide, its volume, which
   camera it looks through, a remembered ?flag). Those keys, and only those, are the persona's:
   - At load, before any page code reads them, the box's are written into this phone's
     localStorage and every other one of them is cleared, so a new or wiped phone comes up as the
     same creature. A persona the box has none for yet (prefs null) takes this phone's own, the
     ones it had before personas, never another persona's; if the owner set some before any
     phone sent its own (`adopt`), the phone's own go under the owner's.
   - While it runs, a change to one of them (a setItem, a removeItem, or upstream's own
     `localStorage.key = v`, which no hook sees, so a check every 2 s) is sent a moment later as
     a patch of just the keys that changed, so an owner's edit to another key is never undone.
   - An owner's edit (the poll's `prefs` version passes the last one this tab knows) is pulled
     and written where the creature stands, and announced as `gb-box-prefs` {changed: {key:
     value or null}} for a page that applies it live (the camera does).
   gb_box_prefs is {persona, dirty, version}; a change not yet on the box survives a reload and
   is sent before the box's copy is taken. gb_phone_prefs keeps the phone's own (a G2 session's)
   while it wears a persona, and gives them back. Things about this phone itself (its tilt
   calibration, the consent it gave, its upstream tokens) stay on it. */
(function () {
  "use strict";
  if (location.pathname !== "/v3/next.html") return;
  var PATH = "/api/box/prefs", META = "gb_box_prefs", OWN = "gb_phone_prefs";
  var SEND_AFTER_MS = 1500, CHECK_MS = 2000, RETRY_MS = 15000;
  // Never a secret: pb2_voicetest holds a fish.audio key and gb_travel_cards_v1 a robot's
  // Wi-Fi password, and the box shows a persona's settings to a debug token.
  var KEYS = {
    gb_eye: 1, gb_face_color: 1, gb_face_dir: 1, gb_next_games3: 1, gb_vol: 1, pb2_voice: 1,
    gb_low_power: 1, gb_next_log: 1, pb2_journal: 1, gb_nx_sid: 1, gb_getup: 1, gb_ftdial: 1, gb_ftdial_set: 1,
    gb_fishwa: 1, gb_asfix: 1, gb_judge0: 1, gb_bodysnd: 1, gb_listen: 1, gb_stt: 1,
    gb_rest: 1, gb_legslew: 1
  };
  // Per persona namespace (visual memory on or off) and per robot (keep tracking me).
  var PREFIXES = ["gb_vismem_native/", "gb_ft_latch:"];
  var store;
  try { store = window.localStorage; store.getItem(META); } catch (e) { return; }
  var realSet = Storage.prototype.setItem, realRemove = Storage.prototype.removeItem;
  var soul = window.__gbBoxSoul || {};

  function ours(k) {
    if (Object.prototype.hasOwnProperty.call(KEYS, k)) return true;
    for (var i = 0; i < PREFIXES.length; i++) if (k.indexOf(PREFIXES[i]) === 0) return true;
    return false;
  }
  function snap() {
    var out = {};
    try {
      for (var i = 0; i < store.length; i++) {
        var k = store.key(i);
        if (k != null && ours(k)) out[k] = store.getItem(k);
      }
    } catch (e) {}
    return out;
  }
  function meta() {
    try {
      var m = JSON.parse(store.getItem(META) || "null");
      return m && typeof m === "object" ? m : {};
    } catch (e) { return {}; }
  }
  function setMeta(m) { try { realSet.call(store, META, JSON.stringify(m)); } catch (e) {} }
  // Makes this phone's keys exactly `want`, touching nothing else.
  function become(want) {
    var had = snap();
    Object.keys(had).forEach(function (k) {
      if (!Object.prototype.hasOwnProperty.call(want, k)) { try { realRemove.call(store, k); } catch (e) {} }
    });
    Object.keys(want).forEach(function (k) {
      if (ours(k) && typeof want[k] === "string" && had[k] !== want[k]) {
        try { realSet.call(store, k, want[k]); } catch (e) {}
      }
    });
  }
  function ownSaved() {
    try { return JSON.parse(store.getItem(OWN) || "null"); } catch (e) { return null; }
  }

  if (soul.g2) {
    // A G2 session: the phone's own come back if a persona had the keys.
    // A phone that had none of its own before a persona gets the defaults, never the
    // persona's keys (nor its diary id).
    var own = ownSaved();
    if (meta().persona) become(own && typeof own === "object" ? own : {});
    else if (own && typeof own === "object") become(own);
    if (own || meta().persona) { try { realRemove.call(store, OWN); } catch (e) {} setMeta({}); }
    return;
  }
  var persona = soul.persona;
  if (!persona) return;

  // Synchronous on purpose, as the soul's: the keys must hold the box's before the page reads them.
  function sync(method, body) {
    var r = new XMLHttpRequest();
    try {
      r.open(method, PATH, false);
      if (body) r.setRequestHeader("content-type", "application/json");
      r.send(body || null);
    } catch (e) { return { status: 0, json: null }; }
    var json = null;
    try { json = JSON.parse(r.responseText); } catch (e) {}
    return { status: r.status, json: json };
  }
  function patchOf(from, to) {
    var set = {}, drop = [], n = 0;
    Object.keys(to).forEach(function (k) { if (from[k] !== to[k]) { set[k] = to[k]; n++; } });
    Object.keys(from).forEach(function (k) { if (!Object.prototype.hasOwnProperty.call(to, k)) { drop.push(k); n++; } });
    return n ? { set: set, drop: drop } : null;
  }
  function over(base, p) {
    var out = {};
    Object.keys(base).forEach(function (k) { out[k] = base[k]; });
    Object.keys(p.set).forEach(function (k) { out[k] = p.set[k]; });
    p.drop.forEach(function (k) { delete out[k]; });
    return out;
  }
  function body(p) { return JSON.stringify({ persona: persona, set: p.set, drop: p.drop }); }

  var m = meta();
  // What the box holds, as far as this tab knows: a change is what differs from it.
  var sent = null, version = -1;
  if (!m.persona) {
    // The phone's own keys are set aside, not lost, before a persona's replace them.
    var mine = snap();
    if (Object.keys(mine).length && !ownSaved()) { try { realSet.call(store, OWN, JSON.stringify(mine)); } catch (e) {} }
  }
  var pulled = sync("GET");
  // A cookie that moved to another persona under this tab: nothing of it is taken.
  if (pulled.json && pulled.json.persona !== persona) pulled = { status: 0, json: null };
  var keepMine = false, gapPull = false;
  if (pulled.status === 200 && pulled.json) {
    var box = pulled.json.prefs;
    version = typeof pulled.json.version === "number" ? pulled.json.version : -1;
    var pending = m.persona === persona && m.dirty && m.base && typeof m.base === "object"
      ? patchOf(m.base, snap()) : null;
    if (pending) {
      // A change the box never got: sent now, before the box's copy is taken.
      var got = sync("POST", body(pending));
      if (got.status === 200 && got.json) {
        version = got.json.version;
        var after = sync("GET");
        if (after.status === 200 && after.json && after.json.prefs && after.json.persona === persona) {
          box = after.json.prefs;
          version = after.json.version;
        } else if (box && typeof box === "object") {
          box = over(box, pending);
        }
      } else if (got.status !== 400 && got.status !== 413) {
        // Not taken yet, never refused: the page runs on the box's copy with it laid over, so
        // the owner's edits made meanwhile land too, and the loop below sends it.
        keepMine = true;
      }
    }
    if (keepMine) {
      var held0 = box && typeof box === "object" ? box : m.base;
      become(over(held0, pending));
      sent = {};
      Object.keys(held0).forEach(function (k) { if (ours(k) && typeof held0[k] === "string") sent[k] = held0[k]; });
    } else if (box && typeof box === "object" && pulled.json.adopt === true) {
      // The owner set some before any phone sent its own: this phone's own (never another
      // persona's) go under the owner's, and all of it becomes the persona's.
      var under = m.persona && m.persona !== persona ? {} : (!m.persona && ownSaved()) || snap();
      become(over(under, { set: box, drop: [] }));
      // Only what the box lacks is sent, so an owner edit landing meanwhile is not undone;
      // sent even when empty, since any phone write ends the owner-only mark.
      var mineAll = snap(), extra = {};
      Object.keys(mineAll).forEach(function (k) { if (!Object.prototype.hasOwnProperty.call(box, k)) extra[k] = mineAll[k]; });
      var sentAll = sync("POST", body({ set: extra, drop: [] }));
      if (sentAll.status === 200 && sentAll.json) {
        sent = mineAll;
        if (sentAll.json.version !== version + 1) gapPull = true;
        version = sentAll.json.version;
      }
      else {
        sent = {};
        Object.keys(box).forEach(function (k) { if (ours(k) && typeof box[k] === "string") sent[k] = box[k]; });
      }
    } else if (box && typeof box === "object") {
      become(box);
      sent = snap();
    } else {
      // The first phone this persona opens on: it keeps what it had (its own, not another
      // persona's), and those become the persona's.
      if (m.persona && m.persona !== persona) become({});
      else if (!m.persona && ownSaved()) become(ownSaved());
      var first = snap(), all = sync("POST", body({ set: first, drop: [] }));
      if (all.status === 200 && all.json) { sent = first; version = all.json.version; }
      else sent = {};
    }
    setMeta({ persona: persona, dirty: keepMine || !!patchOf(sent, snap()), base: sent, version: version });
  } else if (m.persona === persona) {
    // The box did not answer: the creature runs on what this phone last had of it.
    sent = m.base && typeof m.base === "object" ? m.base : {};
    version = typeof m.version === "number" ? m.version : -1;
  } else {
    // Nothing of this persona's is known here: it runs on the defaults (never another
    // persona's keys) and sends nothing, so they never overwrite what the box holds.
    if (m.persona) become({});
    return;
  }

  var timer = 0, inflight = false, held = false, retry = 0;
  function schedule(ms) { clearTimeout(timer); timer = setTimeout(push, ms); }
  function check() {
    if (held || !patchOf(sent, snap())) return;
    var now = meta();
    if (now.persona !== persona) { held = true; return; }
    if (!now.dirty) { now.dirty = true; setMeta(now); }
    // A retry already waiting keeps its backoff: a busy box is not asked every check.
    if (!inflight && !retry) schedule(SEND_AFTER_MS);
  }
  function push(keepalive) {
    if (held) return;
    if (inflight) { schedule(SEND_AFTER_MS); return; }
    var now = snap(), p = patchOf(sent, now);
    // Nothing left to send ends any backoff, so the next change is sent on time.
    if (!p) { retry = 0; return; }
    if (meta().persona !== persona) { held = true; return; }
    inflight = true;
    var before = version;
    fetch(PATH, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: body(p),
      keepalive: !!keepalive
    }).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (j) { return { status: r.status, json: j }; });
    }).then(function (r) {
      inflight = false;
      if (r.status === 200 && r.json) {
        sent = now; retry = 0;
        if (typeof r.json.version === "number") version = r.json.version;
        setMeta({ persona: persona, dirty: !!patchOf(sent, snap()), base: sent, version: version });
        // Someone else (the owner, another tab) wrote in between: theirs is pulled now, since
        // the poll's version will not read as newer than this one.
        if (typeof r.json.version === "number" && r.json.version !== before + 1) pullOwner();
        check();
      } else if (r.status === 400 || r.status === 413) {
        // The box will never keep this change: it is dropped (the next load takes the box's).
        sent = now; retry = 0;
        setMeta({ persona: persona, dirty: false, base: sent, version: version });
      } else if (r.status === 404 || r.status === 409) {
        held = true;   // another phone, another persona, or deleted: the soul's poll reloads
      } else {
        retry = Math.min(retry ? retry * 2 : RETRY_MS, 300000);
        schedule(retry);
      }
    }).catch(function () {
      inflight = false;
      retry = Math.min(retry ? retry * 2 : RETRY_MS, 300000);
      schedule(retry);
    });
  }

  // The owner changed them from JBrain: taken where the creature stands, keys this tab changed
  // and has not yet sent kept.
  function pullOwner() {
    fetch(PATH, { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      if (!j || !j.prefs || typeof j.prefs !== "object" || held) return;
      if (j.persona !== persona) { held = true; return; }
      var mineNow = snap(), mineP = patchOf(sent, mineNow) || { set: {}, drop: [] };
      var want = {}, changed = {}, any = false;
      Object.keys(j.prefs).forEach(function (k) { if (ours(k) && typeof j.prefs[k] === "string") want[k] = j.prefs[k]; });
      Object.keys(mineP.set).forEach(function (k) { want[k] = mineP.set[k]; });
      mineP.drop.forEach(function (k) { delete want[k]; });
      Object.keys(want).forEach(function (k) { if (mineNow[k] !== want[k]) { changed[k] = want[k]; any = true; } });
      Object.keys(mineNow).forEach(function (k) { if (!Object.prototype.hasOwnProperty.call(want, k)) { changed[k] = null; any = true; } });
      become(want);
      sent = {};
      Object.keys(j.prefs).forEach(function (k) { if (ours(k)) sent[k] = j.prefs[k]; });
      version = j.version;
      setMeta({ persona: persona, dirty: !!patchOf(sent, snap()), base: sent, version: version });
      if (any) { try { window.dispatchEvent(new CustomEvent("gb-box-prefs", { detail: { changed: changed } })); } catch (e) {} }
      check();
    }).catch(function () {});
  }
  window.addEventListener("gb-box-state", function (ev) {
    var v = ev && ev.detail && ev.detail.prefs;
    if (typeof v === "number" && v > version && !inflight) pullOwner();
  });

  Storage.prototype.setItem = (function (inner) {
    return function (key) {
      var out = inner.apply(this, arguments);
      if (this === store && ours(String(key))) check();
      return out;
    };
  })(Storage.prototype.setItem);
  Storage.prototype.removeItem = (function (inner) {
    return function (key) {
      var out = inner.apply(this, arguments);
      if (this === store && ours(String(key))) check();
      return out;
    };
  })(Storage.prototype.removeItem);
  setInterval(check, CHECK_MS);
  document.addEventListener("visibilitychange", function () { if (document.hidden) { check(); push(true); } });
  window.addEventListener("pagehide", function () { check(); push(true); });
  if (meta().dirty) check();
  if (gapPull) pullOwner();
})();

/* A persona's visual memories live on the box too (plan §2c, wave G4d). Upstream keeps them in
   IndexedDB (v3/visual-memory.js), under vmNs(); apply.py makes vmNs() read this tab's
   __gbBoxVmNs first, pinned here to the persona, so each persona's saved views are its own on
   a shared phone.
   - At load, before the creature opens the store (apply.py makes vmOpen wait on
     gbBoxMemReady), the box's records are merged in: the phone and the box are compared record
     by record against `known`, what both agreed on last, so each side's own changes win and an
     owner's delete or restore lands. Other personas' records leave the phone; the box has them.
   - While it runs, every write the creature makes to the store (apply.py calls
     gbBoxMemChanged) is sent a moment later: a new or changed record whole, and the ids it
     forgot. A record the box's changed since, or the poll's newer `memories`, means the owner
     changed them, and the page reloads when idle to take the box's.
   - `dirty` marks writes not yet on the box. Only then is a record the phone lacks one it
     forgot; otherwise the phone's store lost it, and the box's copy comes back.
   gb_box_vm/<persona> is {known: {id: fingerprint}, version, dirty (a count), missing (records
   the merge could not fetch), mergedAt (the box version the last merge finished)}. */
(function () {
  "use strict";
  if (location.pathname !== "/v3/next.html") return;
  var PREFIX = "gb_box_vm/", PATH = "/api/box/memories";
  var READY_MS = 8000, SEND_AFTER_MS = 2000, RETRY_MS = 15000, RETRY_MAX_MS = 300000;
  var IDLE_MS = 20000;
  var store;
  try { store = window.localStorage; store.getItem(PREFIX); } catch (e) { return; }
  if (!window.indexedDB || !window.Promise) return;
  var soul = window.__gbBoxSoul || {}, persona = soul.persona;
  var leaving = !!(soul.g2 || soul.away);
  if (!persona && !leaving) return;
  var NS = "gb-" + (persona || ""), KEY = PREFIX + (persona || "");

  function mine() {
    try {
      var m = JSON.parse(store.getItem(KEY) || "null");
      if (m && typeof m === "object" && m.known && typeof m.known === "object") return m;
    } catch (e) {}
    return { known: {}, version: -1, dirty: 0, missing: {} };
  }
  function save(m) { try { store.setItem(KEY, JSON.stringify(m)); } catch (e) {} }
  // What the phone and the box compare: the creature's fields and its views' times. The
  // pictures in a view never change once taken, so a view's time names it.
  function fp(r) {
    return JSON.stringify([r.kind, r.name, r.desc, r.t, r.confirmed, r.namedAt,
      (r.views || []).map(function (v) { return v && v.t; })]);
  }
  function call(method, path, body) {
    return fetch(path, {
      method: method,
      headers: body ? { "content-type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (j) { return { status: r.status, json: j }; });
    });
  }
  function done(q) {
    return new Promise(function (res, rej) {
      q.onsuccess = function () { res(q.result); };
      q.onerror = function () { rej(q.error || new Error("storage")); };
    });
  }
  function finished(tx) {
    return new Promise(function (res, rej) {
      tx.oncomplete = function () { res(); };
      tx.onerror = tx.onabort = function () { rej(tx.error || new Error("storage")); };
    });
  }

  var db = null;
  // The creature's own database, made the way visual-memory.js makes it when it is new. One
  // from before its meta store is left for the creature to upgrade, and synced next load.
  function open() {
    if (db) return Promise.resolve(db);
    return new Promise(function (res) {
      var q;
      try { q = indexedDB.open("gb_vismem", 2); } catch (e) { res(null); return; }
      q.onupgradeneeded = function (ev) {
        var d = q.result;
        if (ev.oldVersion !== 0) { q.transaction.abort(); return; }
        ["refs", "runs", "meta"].forEach(function (name) {
          d.createObjectStore(name, { keyPath: "key" }).createIndex("ns", "ns", { unique: false });
        });
      };
      q.onsuccess = function () {
        db = q.result;
        db.onversionchange = function () { try { db.close(); } catch (e) {} db = null; };
        res(db);
      };
      q.onerror = q.onblocked = function () { res(null); };
    });
  }
  // A persona's records, but for `keep`'s, leave the phone: the box has them.
  function purge(d, keep) {
    var tx = d.transaction(["refs", "meta", "runs"], "readwrite");
    ["refs", "meta", "runs"].forEach(function (name) {
      var q = tx.objectStore(name).index("ns").openKeyCursor();
      q.onsuccess = function () {
        var c = q.result;
        if (!c) return;
        if (typeof c.key === "string" && c.key.indexOf("gb-") === 0 && c.key !== keep) tx.objectStore(name).delete(c.primaryKey);
        c.continue();
      };
    });
    return finished(tx).then(function () {
      Object.keys(store).forEach(function (k) {
        if (k.indexOf(PREFIX) === 0 && k !== PREFIX + keep.slice(3)) { try { store.removeItem(k); } catch (e) {} }
      });
    });
  }
  if (leaving) {
    // Moved to another phone, deleted, or a G2 session: no persona's faces stay here. A G2
    // answer can also be a session that lapsed, so faces not yet on the box wait for the
    // persona to be opened again.
    var unsent = soul.g2 && Object.keys(store).some(function (k) {
      if (k.indexOf(PREFIX) !== 0) return false;
      try { return +(JSON.parse(store.getItem(k) || "{}").dirty) > 0; } catch (e) { return false; }
    });
    if (!unsent) open().then(function (d) { if (d) return purge(d, ""); }).catch(function () {});
    return;
  }
  // Per tab, never shared: a tab still open on the persona this phone held before keeps
  // that persona's namespace, so it cannot save a face into the new one.
  window.__gbBoxVmNs = NS;
  function local(d) {
    return done(d.transaction("meta", "readonly").objectStore("meta").index("ns").getAll(NS)).then(function (rows) {
      var out = {};
      (rows || []).forEach(function (r) { if (r && r.ns === NS && r.key === NS + "/" + r.id) out[r.id] = r; });
      return out;
    });
  }
  function boxList() {
    return call("GET", PATH).then(function (r) {
      if (r.status !== 200 || !r.json || !Array.isArray(r.json.memories)) throw r;
      var out = {};
      r.json.memories.forEach(function (m) { if (m && typeof m.id === "string") out[m.id] = m; });
      return { version: r.json.version, rows: out };
    });
  }
  function picture(id, i) {
    return call("GET", PATH + "/view?id=" + encodeURIComponent(id) + "&i=" + i).then(function (r) {
      if (r.status !== 200 || !r.json || typeof r.json.j !== "string") throw r;
      return r.json.j;
    });
  }
  function rows(b, views) {
    var meta = { key: NS + "/" + b.id, ns: NS, id: b.id, v: 1, kind: b.kind, name: b.name, desc: b.desc,
      t: b.t, confirmed: b.confirmed, namedAt: b.namedAt, views: views.map(function (v) { return { t: v.t }; }) };
    var ref = {};
    for (var k in meta) ref[k] = meta[k];
    ref.views = views;
    return { meta: meta, ref: ref };
  }

  // 1. The merge, before the creature reads its store. Each record lands, and is known, as
  //    soon as its pictures are in, so a slow box never costs a load all of its work.
  var late = false, merging = true, wroteLate = false;
  function put(d, r) {
    var tx = d.transaction(["refs", "meta"], "readwrite");
    tx.objectStore("refs").put(r.ref);
    tx.objectStore("meta").put(r.meta);
    return finished(tx).then(function () {
      var now = mine();
      now.known[r.meta.id] = fp(r.meta);
      delete (now.missing || {})[r.meta.id];
      save(now);
      if (late) wroteLate = true;
    });
  }
  function merge() {
    return Promise.all([open(), boxList()]).then(function (got) {
      var d = got[0], box = got[1];
      if (!d) return;
      return local(d).then(function (here) {
        var m = mine(), known = m.known, take = [], drop = [];
        Object.keys(box.rows).forEach(function (id) {
          var b = box.rows[id], l = here[id];
          if (l) {
            if (fp(l) !== fp(b) && fp(b) !== known[id]) take.push(b);
            else if (fp(l) === fp(b)) known[id] = fp(b);
          } else if (known[id] === undefined || !m.dirty) {
            take.push(b);
          }
        });
        Object.keys(here).forEach(function (id) {
          if (!box.rows[id] && known[id] !== undefined) drop.push(id);
        });
        var tx = d.transaction(["refs", "meta"], "readwrite");
        drop.forEach(function (id) { tx.objectStore("refs").delete(NS + "/" + id); tx.objectStore("meta").delete(NS + "/" + id); });
        return finished(tx).then(function () {
          drop.forEach(function (id) { delete known[id]; });
          m.missing = m.missing || {};
          Object.keys(m.missing).forEach(function (id) { if (!box.rows[id]) delete m.missing[id]; });
          m.version = box.version;
          save(m);
          // Another persona's faces are on the box, not this phone's to keep.
          return purge(d, NS);
        }).then(function () {
          // Pictures one record at a time: a transaction would not survive the network.
          return take.reduce(function (p, b) {
            return p.then(function () {
              var views = Array.isArray(b.views) ? b.views : [];
              return Promise.all(views.map(function (v, i) {
                return picture(b.id, i).then(function (j) { return { j: j, t: v.t }; });
              })).then(function (vs) {
                if (vs.length) return put(d, rows(b, vs));
              }).catch(function () {
                // Not to be had now (a picture the box lacks, a busy box): not a reason to
                // reload, and tried again at the next load.
                var now = mine();
                now.missing = now.missing || {};
                now.missing[b.id] = fp(b);
                save(now);
              });
            });
          }, Promise.resolve()).then(function () {
            // This version was merged: what it could not bring is no reason to reload.
            var now = mine();
            now.mergedAt = box.version;
            save(now);
          });
        });
      });
    }).catch(function () {}).then(function () {
      merging = false;
      if (wroteLate) {
        // The creature read its store before these landed: it reads it again, once.
        try {
          window._vmLoaded = false;
          window._vmStoreEpoch = (window._vmStoreEpoch || 0) + 1;
          if (typeof window.vmLoad === "function") window.vmLoad().catch(function () {});
        } catch (e) {}
      }
    });
  }
  var merged = merge();
  window.gbBoxMemReady = Promise.race([
    merged,
    new Promise(function (res) { setTimeout(function () { late = true; res(); }, READY_MS); }),
  ]);

  // 2. The creature's own writes, sent a moment after the last one.
  var timer = 0, busy = false, again = false, refused = {}, stopped = false, lastInput = Date.now();
  var retry = RETRY_MS;
  function reloadWhenIdle() {
    stopped = true;
    if (document.hidden || Date.now() - lastInput > IDLE_MS) location.reload();
    else setTimeout(reloadWhenIdle, 5000);
  }
  function away(r) {
    // Moved, deleted, or a persona this tab no longer runs as: the soul's loader reloads.
    if (r.status === 404 || r.status === 409) { stopped = true; return true; }
    return false;
  }
  function send() {
    if (stopped) return;
    if (busy) { again = true; return; }
    busy = true; again = false;
    var failed = false, at = mine().dirty;
    Promise.all([open(), boxList()]).then(function (got) {
      var d = got[0], box = got[1];
      if (!d) return;
      return local(d).then(function (here) {
        var m = mine(), known = m.known, missing = m.missing || {}, puts = [], forgets = [];
        var theirs = false;
        Object.keys(here).forEach(function (id) {
          var l = here[id], b = box.rows[id];
          if (refused[id] === fp(l)) return;
          if (!b) { if (known[id] !== undefined) theirs = true; else puts.push(id); }
          else if (fp(l) !== fp(b)) { if (fp(b) !== known[id]) theirs = true; else puts.push(id); }
          else known[id] = fp(b);
        });
        Object.keys(box.rows).forEach(function (id) {
          if (here[id]) return;
          if (known[id] !== undefined && m.dirty) forgets.push(id);
          // A record the merge is still fetching, or could not, is no reason to reload.
          else if (!merging && missing[id] !== fp(box.rows[id])) theirs = true;
        });
        m.version = box.version;
        // A version this load merged has nothing more to bring (what it could not fetch waits
        // for the next load), so it never reloads the page; a newer one does.
        if (m.mergedAt === box.version) theirs = false;
        save(m);
        return puts.reduce(function (p, id) {
          return p.then(function () {
            if (stopped) return;
            return done(d.transaction("refs", "readonly").objectStore("refs").get(NS + "/" + id)).then(function (row) {
              if (!row || !Array.isArray(row.views)) return;
              var record = { id: row.id, kind: row.kind, name: row.name, desc: row.desc, t: row.t,
                confirmed: row.confirmed, namedAt: row.namedAt,
                views: row.views.map(function (v) { return { j: v.j, t: v.t }; }) };
              return call("POST", PATH, { persona: persona, memory: record }).then(function (r) {
                if (r.status === 200) {
                  var now = mine(); now.known[id] = fp(record); now.version = r.json && r.json.version; save(now);
                } else if (r.status === 400 || r.status === 413) {
                  refused[id] = fp(record);   // the box will never keep it: not sent again
                } else if (!away(r)) failed = true;
              });
            });
          });
        }, Promise.resolve()).then(function () {
          if (stopped || !forgets.length) return;
          return call("POST", PATH + "/forget", { persona: persona, ids: forgets }).then(function (r) {
            if (r.status === 200) {
              var now = mine();
              forgets.forEach(function (id) { delete now.known[id]; });
              now.version = r.json && r.json.version; save(now);
            } else if (!away(r)) failed = true;
          });
        }).then(function () {
          if (theirs) reloadWhenIdle();
        });
      });
    }).catch(function () { failed = true; }).then(function () {
      busy = false;
      // A write made while this ran, in this tab or another, is not on the box yet.
      if (!failed && !again) { var now = mine(); if (now.dirty === at) { now.dirty = 0; save(now); } }
      if (!failed) retry = RETRY_MS;
      if (again) send();
      else if (failed && !stopped) {
        clearTimeout(timer);
        timer = setTimeout(send, retry);
        retry = Math.min(retry * 2, RETRY_MAX_MS);
      }
    });
  }
  window.gbBoxMemChanged = function () {
    // A count, shared by this phone's tabs: a send clears it only if nothing wrote since.
    var m = mine();
    m.dirty = (+m.dirty || 0) + 1;
    save(m);
    clearTimeout(timer);
    timer = setTimeout(send, SEND_AFTER_MS);
  };
  merged.then(send);
  window.addEventListener("gb-box-state", function (ev) {
    var got = ev.detail && ev.detail.memories;
    if (typeof got === "number" && got !== mine().version && !busy) send();
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden && mine().dirty && !busy) send();
  });
  ["pointerdown", "keydown", "touchstart"].forEach(function (type) {
    window.addEventListener(type, function () { lastInput = Date.now(); }, { capture: true, passive: true });
  });
})();

/* Pairing is JBrain's (plan §2c): the relay refuses a code typed into the creature, so its own
   pairing box (a prompt, behind Pair and Reconnect) says where pairing is done instead. */
(function () {
  "use strict";
  var realPrompt = window.prompt;
  window.prompt = function (message) {
    if (/pairing code/i.test(String(message))) {
      var note = document.createElement("div");
      note.textContent = "Robots are paired from JBrain, on its GrowBot screen.";
      note.setAttribute("role", "status");
      note.style.cssText =
        "position:fixed;left:16px;right:16px;bottom:24px;z-index:2147483647;padding:12px 16px;" +
        "border-radius:10px;background:#151a2b;color:#e6edf3;border:1px solid #7fd89a;" +
        "font:16px/1.4 system-ui,sans-serif;text-align:center";
      document.body.appendChild(note);
      setTimeout(function () { note.remove(); }, 4000);
      return null;
    }
    return realPrompt.apply(window, arguments);
  };
})();

/* Why the box failed the creature, in words. Upstream's lines were written for cloud services
   (its "my ears are down", "brain error: empty stream", a bare abort message), so a local model
   that was slow, not running, or refused the turn all read the same. The page's own failure
   lines ask these for the box's reason (apply.py ERROR_PATCHES). */
(function () {
  "use strict";
  var real = window.fetch;
  if (typeof real !== "function") return;
  var ears = { why: "", t0: 0 };
  var EARS = /^\/api\/llm\?target=(groq|deepinfra)-audio(&|$)/;

  function secs(ms) { return Math.max(1, Math.round(ms / 1000)); }
  function aborted(e) {
    return !!e && (e.name === "AbortError" || /abort/i.test(String(e.message || "")));
  }
  function unreachable(e) {
    return !!e && e.name === "TypeError" && /fetch|load failed|network/i.test(String(e.message || ""));
  }

  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    if (!EARS.test(url)) return real.apply(this, arguments);
    var t0 = Date.now();
    ears = { why: "", t0: t0 };
    return real.apply(this, arguments).then(function (r) {
      if (r.ok) return r;
      // Read before the page sees the response: its catch speaks the moment it throws.
      return r.clone().json().then(function (j) {
        var said = j && (typeof j.error === "string" ? j.error : j.error && j.error.message);
        ears.why = typeof said === "string" && said ? said : "the box's speech model answered HTTP " + r.status;
        return r;
      }, function () {
        ears.why = "the box's speech model answered HTTP " + r.status;
        return r;
      });
    }, function (e) {
      ears.why = aborted(e)
        ? "the box's speech model took over " + secs(Date.now() - t0) + " s, so I stopped waiting"
        : "I couldn't reach the box";
      throw e;
    });
  };

  window.gbBoxEars = function (e) {
    // Another tab owns the soul: the page's held fetch never reached the box, and says so.
    if (e && /^held:/.test(String(e.message || ""))) {
      return "my ears are with another tab: this creature is open there too.";
    }
    var why = ears.why;
    if (!why && aborted(e)) {
      why = "the box's speech model took over " + secs(Date.now() - ears.t0) + " s, so I stopped waiting";
    }
    return "my ears are down: " + (why || "the box's speech model failed") + ". try again, or type.";
  };

  window.gbBoxBrainWhy = function (e) {
    var m = String((e && e.message) || e || "");
    if (e && typeof e.boxSaid === "string") return "the box said: " + e.boxSaid;
    // No number: a spoken turn waits 90 s (apply.py ERROR_PATCHES), a tick 30.
    if (aborted(e)) return "the box's model didn't answer in time, so I stopped waiting";
    if (unreachable(e)) return "I couldn't reach the box";
    if (m === "empty stream") return "the box's model answered with nothing";
    var http = /^HTTP (\d+)\s*(.*)$/.exec(m);
    if (http) return "the box answered HTTP " + http[1] + (http[2] ? " (" + http[2] + ")" : "");
    return m;
  };
})();

// What changes every turn rides in the turn's own message, so the system message stays the
// same text from one turn to the next and the box resumes its cached prompt at the system's
// end instead of re-reading it whole (apply.py SPEED_PATCHES). False hands it back to the
// page, which then keeps upstream's layout.
(function () {
  var HEAD = "== WHERE THINGS STAND (your memory and state right now; context, not words anyone said) ==\n";
  window.gbBoxLive = function (msgs, live) {
    var last = msgs && msgs[msgs.length - 1];
    if (!last || last.role !== "user") return false;
    var text = String(live || "").replace(/^\s+/, "");
    if (!text) return true;
    var head = HEAD + text + "\n\n== THIS MOMENT ==\n";
    if (typeof last.content === "string") last.content = head + last.content;
    else if (Array.isArray(last.content)) last.content = [{ type: "text", text: head }].concat(last.content);
    else return false;
    return true;
  };
})();

// Readable on a phone. Upstream sets the spoken line at 11px and the brain panel at 11-12.5px,
// sized for its own pixel font, so the type here is raised to phone sizes. And a phone in
// "desktop site" mode (Safari's Request Desktop Website, Chrome's Desktop site) ignores the
// viewport tag and lays the page out ~980px wide, shrinking everything by ~2.3x: that layout is
// scaled back to the screen, the controls by zoom and the spoken line by its size. The face's
// canvas is left alone, since it sizes itself to the window and a zoom would only blow it up.
(function () {
  // The page is run by the box: apply.py hides upstream's cloud-only settings behind this.
  window.__gbBox = true;
  // What those settings left on a phone: a model or effort pick (the box picks the model), an
  // OpenRouter or custom-endpoint key and URL, the credits wall, and the cloud journal's erase
  // and watch link. Dropped before the page reads them, so none of it steers the creature.
  try {
    ["gb_brain", "gb_free_pick", "gb_effort", "pb_byok", "pb_llm_url", "pb_llm_model",
      "pb_llm_key", "pb_llm_vision", "pb_llm_json", "pb_energy_out", "pb2_erase",
      "gb_nx_watch"].forEach(function (k) { window.localStorage.removeItem(k); });
    // The brain log goes to the box, not the cloud, and its only switch was the cloud notes
    // card hidden below: an old opt-out would keep it off for good, so it is lifted.
    ["gb_next_log", "pb2_journal"].forEach(function (k) {
      if (window.localStorage.getItem(k) === "0") window.localStorage.removeItem(k);
    });
  } catch (e) {}
  var style = document.createElement("style");
  function scale() {
    var portrait = window.innerHeight >= window.innerWidth;
    var screenW = portrait
      ? Math.min(screen.width, screen.height)
      : Math.max(screen.width, screen.height);
    var touch = (navigator.maxTouchPoints || 0) > 0;
    var k = touch && screenW > 0 ? window.innerWidth / screenW : 1;
    return k > 1.25 ? Math.min(k, 3) : 1;
  }
  var lastKey = "", lastK = 1;
  function paint() {
    // Re-measure only on a width change or a rotation: an Android keyboard shortens the
    // window past its width, which would read as landscape and drop the zoom mid-typing,
    // while a desktop-site window keeps its ~980px width when the phone turns.
    var turn = (screen.orientation && screen.orientation.type) || String(window.orientation);
    var key = window.innerWidth + "|" + screen.width + "|" + turn;
    var k = key === lastKey ? lastK : scale();
    lastKey = key;
    lastK = k;
    style.textContent =
      "#say{font-size:" + 15 * k + "px!important;line-height:1.6!important}" +
      "#err{font-size:" + 14 * k + "px!important}" +
      // The control bar grows with its zoom; the spoken line sits above it, not under it.
      (k > 1 ? "#say{margin-bottom:" + Math.round(36 + 210 * (k - 1)) + "px!important}" : "") +
      "#gbkBBody{zoom:1.2}" +
      // The credits tab (apply.py SCRUB_PATCHES): a box creature has no credits.
      "[data-gb-cloud]{display:none!important}" +
      // The same cloud rooms reached another way: the money-code badge, and the full
      // panel's key and credits tabs behind the voice row's hand-off.
      "#giftBtn,#bpTabByok,#bpTabCredits{display:none!important}" +
      // The full panel's MIND tab: the soul download and the move to a new phone (the box
      // keeps the soul, and JBrain moves it), the cloud watch link, and the cloud notes card
      // whose delete erased nothing here.
      "#bpmDownloadSoul,#bpmXfer,#bpmXferBox,#bpmWatch,#bpData{display:none!important}" +
      "div:has(>[data-gb-cloud]){grid-template-columns:repeat(2,1fr)!important}" +
      (k > 1 ? "body>*:not(#wrap){zoom:" + k.toFixed(3) + "}" : "");
  }
  window.gbBoxScale = scale;
  paint();
  (document.head || document.documentElement).appendChild(style);
  window.addEventListener("resize", paint);
})();

/* Moves from JBrain (the PWA's Body tab). Walks and spins run on this phone's motion sensors,
   so the box cannot run them itself: while this page wears a robot it joins that robot's side
   channel (/d/<code>-agent, the relay's rider check keeps it to the rider) as `hello`, and each
   {"t":"box_move", rid, move, secs} runs through the creature's own runGait or moveLegs, as if
   its brain had asked, with the walk calibration this phone already has. It answers at once
   with {"t":"box_ack", rid, ok, state, why}: what it started, or the creature's own words for
   why not. Only a visible page joins, since a hidden one stops every move anyway. */
(function () {
  "use strict";
  if (location.pathname !== "/v3/next.html") return;
  var q = new URLSearchParams(location.search);
  var code = q.get("id") || "";
  if (q.get("mode") !== "legs" || !/^gb-[A-Za-z0-9]{1,32}$/.test(code)) return;
  var RETRY_MIN_MS = 2000, RETRY_MAX_MS = 60000;
  var ws = null, retry = RETRY_MIN_MS, timer = 0, replaced = false;

  function gone() { return !!(window.__gbBoxSoul && window.__gbBoxSoul.away); }

  function run(move, secs) {
    var spec = null;
    if (move === "forward") spec = { dir: "fwd" };
    else if (move === "back") spec = { dir: "back" };
    else if (move === "spin_left") spec = { gait: "spin_left", dir: "left" };
    else if (move === "spin_right") spec = { gait: "spin_right", dir: "right" };
    else if (move === "stop") spec = { dir: "stop" };
    if (spec) {
      if (typeof window.runGait !== "function") throw new Error("the creature has not loaded its legs yet");
      if (secs > 0 && move !== "stop") spec.secs = secs;
      window.runGait(spec);
    } else if (move === "wiggle" || move === "bow" || move === "sway") {
      if (typeof window.moveLegs !== "function") throw new Error("the creature has not loaded its legs yet");
      window.moveLegs({ gesture: move });
    } else {
      throw new Error("not a move this creature knows");
    }
  }

  function answer(rid, move, secs) {
    var err = document.getElementById("err");
    // setErr also retires a "try again" left under the old caption.
    if (typeof window.setErr === "function") window.setErr(""); else if (err) err.textContent = "";
    var before = window._moveLast, out = { t: "box_ack", rid: rid, ok: false, state: "", why: "" };
    try {
      run(move, secs);
      if (move === "stop") {
        // runGait files a halt word as a move it never admitted; the legs did stop.
        out.ok = true;
        out.state = "stopped";
        return out;
      }
      var a = window._moveLast;
      var mine = a && a !== before ? a : null;
      out.state = mine ? String(mine.state || "") : "";
      out.why = (err && err.textContent) || (mine && mine.reason) || "";
      // A move still only "requested" sent nothing to the legs (no body on this page).
      out.ok = !!mine && /^(accepted|started|completed)$/.test(out.state);
      if (!out.ok && !out.why) out.why = "the robot's legs aren't connected to this page";
    } catch (e) {
      out.why = String((e && e.message) || e);
    }
    out.why = String(out.why).slice(0, 220);
    return out;
  }

  function close() {
    clearTimeout(timer);
    timer = 0;
    if (ws) { var s = ws; ws = null; try { s.close(1000); } catch (e) {} }
  }

  function later() {
    clearTimeout(timer);
    if (document.hidden || replaced || gone()) return;
    timer = setTimeout(open, retry);
    retry = Math.min(retry * 2, RETRY_MAX_MS);
  }

  function open() {
    timer = 0;
    if (ws || document.hidden || replaced || gone()) return;
    var s;
    try {
      s = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host +
        "/d/" + encodeURIComponent(code) + "-agent");
    } catch (e) { later(); return; }
    ws = s;
    s.onopen = function () {
      retry = RETRY_MIN_MS;
      try { s.send(JSON.stringify({ t: "hello", box: "moves" })); } catch (e) {}
    };
    s.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (!m || m.t !== "box_move" || typeof m.rid !== "string") return;
      // A page that has since gone away (opened on another phone) takes no more moves.
      if (gone()) { close(); return; }
      // The box's bounds again, so a frame from another of the rider's sockets gets no longer walk.
      var secs = typeof m.secs === "number" && m.secs >= 1 && m.secs <= 15 ? m.secs : 0;
      try { s.send(JSON.stringify(answer(m.rid, String(m.move || ""), secs))); } catch (e) {}
    };
    s.onclose = function (ev) {
      if (ws !== s) return;
      ws = null;
      // Another tab of this phone took the channel: it keeps it until this one is looked at.
      if (ev && ev.reason === "replaced") replaced = true;
      later();
    };
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { close(); return; }
    replaced = false;
    retry = RETRY_MIN_MS;
    open();
  });
  window.addEventListener("pagehide", close);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", open);
  else open();
})();

/* GrowBot on the box: the robot's leg calibration lives on the box, set from the PWA's Body tab
   (backend api/growbot_body.py), and is copied into this page's gb_walk_cal, where upstream's
   creature, walks, spins and tester pages all read it (calNow(), relay-transport's SWAP). The
   box's copy wins key by key, so a second phone or a reset browser drives the legs the same
   way; a robot nobody calibrated from the PWA (cal null) leaves the phone's own alone. The
   first read is synchronous so a page that reads gb_walk_cal once at load sees it. */
(function () {
  "use strict";
  var PATH = "/api/box/cal", POLL_MS = 5000, KEY = "gb_walk_cal";
  var store;
  try { store = window.localStorage; store.getItem(KEY); } catch (e) { return; }

  function code() {
    var id = "";
    try { id = new URLSearchParams(location.search).get("id") || store.getItem("gb_walk_id") || ""; } catch (e) {}
    id = String(id).trim();
    return /^gb-[A-Za-z0-9]{1,32}$/.test(id) ? id : "";
  }
  function apply(cal) {
    if (!cal || typeof cal !== "object" || Array.isArray(cal)) return;
    var now = {};
    try { now = JSON.parse(store.getItem(KEY) || "{}"); } catch (e) { now = {}; }
    if (!now || typeof now !== "object" || Array.isArray(now)) now = {};
    var changed = false;
    Object.keys(cal).forEach(function (k) {
      if (JSON.stringify(now[k]) !== JSON.stringify(cal[k])) { now[k] = cal[k]; changed = true; }
    });
    if (changed) { try { store.setItem(KEY, JSON.stringify(now)); } catch (e) {} }
  }
  function url(id) { return PATH + "?id=" + encodeURIComponent(id); }

  var first = code();
  if (first) {
    try {
      var x = new XMLHttpRequest();
      x.open("GET", url(first), false);
      x.send();
      if (x.status === 200) apply((JSON.parse(x.responseText) || {}).cal);
    } catch (e) {}
  }
  function poll() {
    var id = code();
    if (!id) return;
    fetch(url(id), { cache: "no-store" }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (j) { if (j) apply(j.cal); }).catch(function () {});
  }
  setInterval(function () { if (!document.hidden) poll(); }, POLL_MS);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });
})();
