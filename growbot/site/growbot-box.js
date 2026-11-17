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
      // The face measures the line this plays to move with the words (growbot-face.js).
      window.__gbBoxAudio = el;
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
  // Whether its quiet moments invite it to explore (apply.py QUIET_TEXT_PATCHES), read at each
  // quiet moment, so a poll's change is the next one's. Anything but false is on.
  if (state.status === 200 && state.json && typeof state.json.explore === "boolean") {
    window.__gbBoxExplore = state.json.explore;
  }
  window.addEventListener("gb-box-state", function (ev) {
    var x = ev && ev.detail && ev.detail.explore;
    if (typeof x === "boolean") window.__gbBoxExplore = x;
  });
  // Whether this phone may use the owner's notes (the brain tools in apply.py): the box says so
  // only for the phone the owner turned it on for, and checks again on every call.
  if (state.status === 200 && state.json && typeof state.json.brain === "boolean") {
    window.__gbBoxBrain = state.json.brain;
  }
  window.addEventListener("gb-box-state", function (ev) {
    var b = ev && ev.detail && ev.detail.brain;
    if (typeof b === "boolean") window.__gbBoxBrain = b;
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
    gb_rest: 1, gb_legslew: 1, gb_talk: 1
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
      if (all.status === 200 && all.json) {
        sent = first;
        // The owner picked one meanwhile: the box kept theirs, so this phone pulls it.
        if (all.json.version !== version + 1) gapPull = true;
        version = all.json.version;
      }
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
   {"t":"box_move", rid, move, secs} runs through the creature's own runGait or moveLegs (or,
   for get_up, its tested get-up) as if its brain had asked, with the walk calibration this phone already has. It answers at once
   with {"t":"box_ack", rid, ok, state, why}: what it started, or the creature's own words for
   why not. It also tells the box which named gestures its body has ({"t":"box_info", gestures}),
   whenever that list changes, so the Body tab can offer a soul's own saved ones as tricks. And
   {"t":"box_steer", rid, drive, heading, speed, pitch} steers the trained walk that a box_move
   with the same `drive` started, answered with a box_ack saying whether that walk still runs
   (gbSteer, below). Only a visible page joins, since a hidden one stops every move anyway. */
(function () {
  "use strict";
  if (location.pathname !== "/v3/next.html") return;
  var q = new URLSearchParams(location.search);
  var code = q.get("id") || "";
  if (q.get("mode") !== "legs" || !/^gb-[A-Za-z0-9]{1,32}$/.test(code)) return;
  var RETRY_MIN_MS = 2000, RETRY_MAX_MS = 60000;
  var POLICY_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  var WALKS = /^(forward|back|spin_left|spin_right|look)$/;
  var DRIVE_RE = /^[0-9a-f]{8,32}$/;
  // upstream's _gestName shape: the only names moveLegs can ever play.
  var GESTURE_RE = /^[a-z0-9_-]+(?: [a-z0-9_-]+)*$/, GESTURES_MAX = 32, INFO_MS = 15000;
  // The creature's own tall pose (next.html STAND_TALL), should this page not define it.
  var TALL = { l: 50, r: 130, ms: 600 };
  var ws = null, retry = RETRY_MIN_MS, timer = 0, replaced = false, info = 0, told = "";

  function gone() { return !!(window.__gbBoxSoul && window.__gbBoxSoul.away); }

  // {look?, forward?, back?} of library policy ids, at least one, nothing else; or "".
  function skillSet(v) {
    if (!v || typeof v !== "object" || Array.isArray(v)) return "";
    var out = {}, n = 0;
    for (var k in v) {
      if (!Object.prototype.hasOwnProperty.call(v, k)) continue;
      if (!/^(look|forward|back)$/.test(k) || typeof v[k] !== "string" || !POLICY_RE.test(v[k])) return "";
      out[k] = v[k]; n++;
    }
    return n ? out : "";
  }

  // "Follow me" by voice (Dave simulator, 2026-11-16): the creature's own follow only leaned
  // its legs after a face, so a person who walked off was lost within seconds, while the Body
  // tab's follower (a library skill set's look walk, gbFollow) kept them in view. The
  // creature's `follow:"me"` starts that same follower, on the skill set the box offers.
  // Asked again while it follows, it carries on: a restart would drop the face it holds (or
  // the search for one it lost) and stop the legs to re-stand. A stop that lands while the
  // skill set loads (the motion generation moves on) starts nothing.
  var followAskT = -Infinity;
  // `since`: when the reply's thought was issued, so a "stop stop" said while the model was
  // still answering refuses the follow it answers with.
  window.gbFollowMe = function (secs, since) {
    if (window.gbFollow && window.gbFollow.active()) return Promise.resolve(true);
    // Nor while one is still loading (the skill set's fetch, the policy): it isn't active yet.
    // A stop since that ask ends the wait, so the next ask is heard.
    if (Date.now() - followAskT < 5000 && !((window._gbFollowStopT || 0) >= followAskT)) return Promise.resolve(true);
    var askT = followAskT = Date.now(), from = Math.min(askT, Number(since) || askT);
    var gen = typeof _motionGen === "number" ? _motionGen : 0;
    return fetch("/api/box/skills?robot=" + encodeURIComponent(code), { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("the box said " + r.status);
        return r.json();
      })
      .then(function (j) {
        var set = skillSet(j && j.skills);
        if (!set || !set.look) throw new Error("no trained look walk to follow with");
        if (typeof _motionGen === "number" && _motionGen !== gen) throw new Error("stopped before it began");
        if ((window._gbFollowStopT || 0) >= from) throw new Error("stopped before it began");
        // A get-up owns the legs until it ends, and a robot lying down can't walk after anyone.
        if (typeof _wireRunner !== "undefined" && _wireRunner && _wireRunner.active) throw new Error("getting up");
        if (typeof _fallSince === "number" && _fallSince > 0) throw new Error("not on my feet");
        run("look", Math.max(1, Math.min(15, Number(secs) || 15)), set, "", "", true);
        return true;
      })
      .catch(function (e) {
        if (followAskT === askT) followAskT = -Infinity;
        try { logEv("follow_me_refused", { why: String((e && e.message) || e).slice(0, 120) }); } catch (_) {}
        return false;
      });
  };


  // The body's gesture names as the creature's prompt sees them (a soul's saved poses included).
  function gestureNames() {
    var lib = null;
    try { lib = typeof body !== "undefined" && body && body.gestures; } catch (e) {}
    if (!lib || typeof lib !== "object") return [];
    return Object.keys(lib).filter(function (k) {
      return k.length <= 16 && GESTURE_RE.test(k) && Array.isArray(lib[k]) && lib[k].length > 0;
    }).sort().slice(0, GESTURES_MAX);
  }

  function tell() {
    if (!ws || ws.readyState !== 1) return;
    var names = gestureNames(), said = JSON.stringify(names);
    if (said === told) return;
    try { ws.send(JSON.stringify({ t: "box_info", gestures: names })); told = said; } catch (e) {}
  }

  function run(move, secs, policy, name, drive, follow) {
    var spec = null;
    // Any other move ends a follow (or its dry run) first; a walk's own stop ends it anyway.
    if (window.gbFollow) window.gbFollow.stop("new_move");
    if (window.gbBodyTest) window.gbBodyTest.stop("new_move");
    if (follow) {
      if (typeof window.gbTrainedWalk !== "function") throw new Error("this page can't follow yet");
      window.gbTrainedWalk(policy, "look", secs, "", true);
      return;
    }
    if (policy) {
      if (typeof window.gbTrainedWalk !== "function") throw new Error("this page can't run a trained walk");
      window.gbTrainedWalk(policy, move, secs, drive);
      return;
    }
    if (move === "look") throw new Error("a trick runs a trained policy, and none was named");
    if (move === "forward") spec = { dir: "fwd" };
    else if (move === "back") spec = { dir: "back" };
    else if (move === "spin_left") spec = { gait: "spin_left", dir: "left" };
    else if (move === "spin_right") spec = { gait: "spin_right", dir: "right" };
    else if (move === "spin_escape_left") spec = { gait: "spin_escape_left", dir: "left" };
    else if (move === "spin_escape_right") spec = { gait: "spin_escape_right", dir: "right" };
    else if (move === "stop") spec = { dir: "stop" };
    if (spec) {
      if (typeof window.runGait !== "function") throw new Error("the creature has not loaded its legs yet");
      if (secs > 0 && move !== "stop") spec.secs = secs;
      // The pad's "Built-in walk" is the stock one, so it stays the A/B against the trained.
      spec.stock = true;
      window.runGait(spec);
    } else if (move === "wiggle" || move === "bow" || move === "sway" || move === "gesture") {
      if (typeof window.moveLegs !== "function") throw new Error("the creature has not loaded its legs yet");
      window.moveLegs({ gesture: move === "gesture" ? name : move });
    } else if (move === "stand_tall") {
      if (typeof window.moveLegs !== "function") throw new Error("the creature has not loaded its legs yet");
      var t = typeof STAND_TALL !== "undefined" && STAND_TALL ? STAND_TALL : TALL;
      window.moveLegs({ body: { steps: [{ l: t.l, r: t.r, ms: t.ms }] } });
    } else if (move === "get_up") {
      // Upstream's tested get-up, from lying screen-up; the page says why when it can't.
      if (typeof window.gbGetUp !== "function") throw new Error("this page can't get up yet");
      window.gbGetUp();
    } else {
      throw new Error("not a move this creature knows");
    }
  }

  // The follower's settings moves (the Body tab's Dry run, Calibrate 1 m and Flip, no legs) and
  // the body test, which drives the legs itself.
  function setting(follow, skills) {
    if (!window.gbFollow) throw new Error("this page can't follow yet");
    if (follow === "test") {
      if (!window.gbBodyTest) throw new Error("this page can't run a body test yet");
      return { state: window.gbBodyTest.start(skills), why: "" };
    }
    // The follower's moves end a body test, as any other move does.
    if (window.gbBodyTest) window.gbBodyTest.stop("new_move");
    if (follow === "dry") {
      // A dry run never shows over walking legs: whatever they were doing stops first.
      window.gbFollow.stop("new_move");
      if (typeof _motion !== "undefined" && _motion) stopMotion();
      return { state: window.gbFollow.dry(skills), why: "" };
    }
    if (follow === "calibrate") {
      window.gbFollow.calibrate(1).then(function (k) { setErr("Measured: you're 1 m away (\u00d7" + k.toFixed(2) + ")."); },
        function (e) { setErr("couldn't measure (" + ((e && e.message) || e) + ")"); });
      return { state: "calibrated", why: "" };
    }
    window.gbFollow.flip().then(function (f) { setErr(f ? "Frames flipped." : "Frames as they come."); },
      function (e) { setErr("couldn't flip (" + ((e && e.message) || e) + ")"); });
    return { state: "flipped", why: "" };
  }

  function answer(rid, move, secs, policy, name, drive, follow) {
    var err = document.getElementById("err");
    // setErr also retires a "try again" left under the old caption.
    if (typeof window.setErr === "function") window.setErr(""); else if (err) err.textContent = "";
    var before = window._moveLast, out = { t: "box_ack", rid: rid, ok: false, state: "", why: "" };
    if (follow && follow !== "walk") {
      try { var got = setting(follow, policy); out.ok = true; out.state = got.state; }
      catch (e) { out.why = String((e && e.message) || e).slice(0, 220); }
      return out;
    }
    try {
      run(move, secs, policy, name, drive, follow === "walk");
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
    clearInterval(info);
    info = 0;
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
      // The soul (and so its saved gestures) may load after this, or change with a new pose.
      told = "";
      tell();
      clearInterval(info);
      info = setInterval(tell, INFO_MS);
    };
    s.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m && m.t === "box_steer" && typeof m.rid === "string") {
        if (gone()) { close(); return; }
        // Answered, so a Drive whose walk has ended (a tip, Stop from elsewhere) hears so.
        var steering = !!(window.gbSteer && window.gbSteer(m));
        try { s.send(JSON.stringify({ t: "box_ack", rid: m.rid, ok: steering, state: steering ? "steering" : "not_driving", why: "" })); } catch (e) {}
        return;
      }
      if (!m || m.t !== "box_move" || typeof m.rid !== "string") return;
      // A page that has since gone away (opened on another phone) takes no more moves.
      if (gone()) { close(); return; }
      // The box's bounds again, so a frame from another of the rider's sockets gets no longer walk.
      var secs = typeof m.secs === "number" && m.secs >= 1 && m.secs <= 15 ? m.secs : 0;
      var move = String(m.move || "");
      var policy = typeof m.policy === "string" && POLICY_RE.test(m.policy) && WALKS.test(move) ? m.policy : "";
      // Or a skill set (growbot-drive.js): a library policy for each of look, forward and back.
      if (!policy && WALKS.test(move)) policy = skillSet(m.skills);
      // The Body tab's Drive names its walk, so only that walk takes its steering.
      var drive = policy && typeof m.drive === "string" && DRIVE_RE.test(m.drive) ? m.drive : "";
      var name = move === "gesture" && typeof m.name === "string" && m.name.length <= 16 && GESTURE_RE.test(m.name) ? m.name : "";
      // The follower (gbFollow): a skill set's "look", followed, dry run, or a camera setting.
      var follow = /^(walk|dry|calibrate|flip|test)$/.test(String(m.follow || "")) && move === "look" &&
        !!policy && typeof policy === "object" ? String(m.follow) : "";
      if (m.follow && !follow) {
        try { s.send(JSON.stringify({ t: "box_ack", rid: m.rid, ok: false, state: "", why: "following takes a skill set's look" })); } catch (e) {}
        return;
      }
      if (move === "gesture" && !name) {
        try { s.send(JSON.stringify({ t: "box_ack", rid: m.rid, ok: false, state: "", why: "not a gesture name this creature can play" })); } catch (e) {}
        return;
      }
      try { s.send(JSON.stringify(answer(m.rid, move, secs, policy, name, drive, follow))); } catch (e) {}
      tell();
    };
    s.onclose = function (ev) {
      if (ws !== s) return;
      ws = null;
      clearInterval(info);
      info = 0;
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

/* Trained walks from JBrain's policy library (the Body tab's pad with a policy picked; backend
   api/growbot_policies.py). GrowBot_Training's walks are steered by a heading, not upstream's
   constant turn word, so they run here rather than through runGait (README "Getting a policy
   onto the robot", and the training thread's contract, 2026-11-14):
   - The file loads from the box (/api/box/policy) into upstream's own Walker
     (growbot_runner.js), ticked at its control_hz, reset at the walk's start.
   - Legs neutral (with this phone's trims) for 1 s first, as the simulator starts each episode.
   - The IMU in the body frame: x toward the nose (the phone's top), y to the robot's left, z
     out of the screen, whichever end the walk leads with (a camera walk only measures its
     heading from the tail, which the relative target below makes no difference to); so accel (phone.y, -phone.x, phone.z) and gyro
     (beta, -alpha, gamma) in rad/s, as upstream's forward walk maps them. Then levelled by the
     smallest rotation taking the gravity measured at the settled stance to +z, with IMU_SIGN
     [1, 1, 1] and gain 1, as trained; the Walker is pinned to z-up so it never rotates again.
   - The heading is the levelled gyro's z rate integrated from the start (counter-clockwise
     positive), and each tick cmd_yaw = wrap180(target - heading) / 180, positive = turn left.
     Training adds drift and noise to that, and the facing end does not change the sum, since the
     target is set relative to where the facing end points at the start.
   - The pad's moves are targets: forward 0 at full speed, left +90 and right -90 (standing to
     turn when it takes a speed), back -1 speed where it takes one, else a turn around (180),
     which keeps turning left: at ±180 exactly the error's sign would flip with the noise.
   - The Body tab's steering (box_steer, a few a second while it drives) replaces the pad's
     target, speed and camera height of the walk its Drive started (the same `drive` id; no
     other walk, a pad move or one from elsewhere, ever takes it), and keeps the walk
     going STEER_HOLD_MS past the latest one, up to DRIVE_MAX_MS, spending the duty budget as
     it goes: the drive ends a moment after the tab stops steering, whatever ends the tab.
   - Every stop upstream's walks have: Stop and any newer move (stopMotion / carryLegMotion own
     _motion), pause, hide, a dream, a lost relay, a tip or a grab, the tip cool-off, and the
     legs' duty budget (dutyLeftMs / policySeconds / _walkLog, as runGait grants it). */
(function () {
  "use strict";
  // The page keeps a policy as long as the box does (serve.py POLICY_CACHE_S), so one deleted
  // from the library stops walking within a minute rather than at the next reload.
  var D = Math.PI / 180, PREP_MS = 1000, KEEP_MS = 60000, cache = {};
  var STEER_HOLD_MS = 1500, DRIVE_MAX_MS = 120000, steer = null;
  // A skill set's file swap: legs neutral this long first, as each file's episodes start.
  var SETTLE_MS = 400;

  // True when the walk this steer's drive started still runs here (and so takes it).
  window.gbSteer = function (m) {
    function ok(v, lim) { return typeof v === "number" && isFinite(v) && Math.abs(v) <= lim; }
    if (!m || !ok(m.heading, 180) || !ok(m.speed, 1) || !ok(m.pitch, 1)) return false;
    var M = _motion;
    if (!M || !M.drive || M.drive !== m.drive) return false;
    steer = { drive: m.drive, heading: m.heading, speed: m.speed, pitch: m.pitch, t: Date.now() };
    return true;
  };

  /* The face follower (growbot-follow.js, wave P3 of docs/plans/GROWBOT_POLICY_DRIVE_PLAN.md).
     Every face box the eyes see (growbot-eyes.js, whichever detector made it) becomes a reading;
     one person is locked and kept; each tick the follower's command steers the skill set in
     place of the Drive's. A dry run does all of it with the legs left alone and shows the
     command on the creature's own screen, which faces the person it follows. The camera's
     numbers (its width of view, the 1 m correction, which way its frames run) live on this
     phone, set from the Body tab (follow "calibrate" and "flip"). */
  // A follow outlasts a Drive (owner, 2026-11-17: "2 minutes is too short"): a walk round the
  // house, not a nudge across the room.
  var CAM_KEY = "gb.follow.camera", FOLLOW_MAX_MS = 300000, LOCK_WAIT_MS = 6000, LOST_END_MS = 8000;
  var follow = null, seen = { boxes: [], t: -Infinity }, followLib = null, ui = null, uiIv = 0;
  // Bumped by every stop, so a follower still loading when a stop lands never starts after it.
  var followGen = 0;
  function frontCamera() { return !!window.camOn && window.camFacing === "user"; }

  function followLoad() {
    if (!followLib) followLib = import("/growbot-follow.js").catch(function (e) { followLib = null; throw e; });
    return followLib;
  }
  function camera(lib) {
    var c = {};
    try { c = JSON.parse(localStorage.getItem(CAM_KEY) || "{}") || {}; } catch (e) { c = {}; }
    return Object.assign({}, lib.CAMERA, {
      k: c.det === lib.DETECTOR && typeof c.k === "number" && c.k > 0.2 && c.k < 5 ? c.k : lib.CAMERA.k,
      flipX: c.flipX === true,
      det: lib.DETECTOR,
    });
  }
  function saveCamera(c) {
    try { localStorage.setItem(CAM_KEY, JSON.stringify({ k: c.k, flipX: c.flipX, det: c.det })); } catch (e) {}
  }
  function goodBox(b) { return b && isFinite(b.x) && isFinite(b.y) && b.width > 0 && b.height > 0; }

  function followBoxes(boxes, src, ms) {
    // Only the front camera's frames: a face seen by the back eye is behind it, signs reversed.
    if (!frontCamera()) { seen = { boxes: [], t: -Infinity }; return; }
    if (src === "tracker") {
      var ftm = window._faceTrack && window._faceTrack.metrics, dms = ftm && ftm.detectMs;
      ms = dms && dms.length ? dms[dms.length - 1] : null;
    }
    var vid = window.camV;
    seen = {
      boxes: (boxes || []).filter(goodBox), t: Date.now(), src: src || "", ms: typeof ms === "number" ? ms : null,
      frame: vid && vid.videoWidth ? [vid.videoWidth, vid.videoHeight] : null,
    };
    var F = follow;
    if (!F) return;
    var reads = seen.boxes.map(function (b) { return F.lib.reading(b, F.cam); }), h = F.heading;
    if (F.track.p) { F.track.update(reads, h, seen.t); return; }
    if (!F.track.lock(reads, h, seen.t)) return;
    F.f.start(F.track.p.dist);
    try { logEv("follow_lock", { dist: Math.round(F.track.p.dist * 100) / 100, bearing: Math.round(wrap180(F.track.p.az - h)), faces: reads.length }); } catch (e) {}
  }
  // growbot-eyes.js may load after this file: join it once it's there.
  (function join() {
    if (window.gbEyes && window.gbEyes.listen) window.gbEyes.listen(followBoxes);
    else setTimeout(join, 500);
  })();

  // Ends the follow running now, or only `own` when given (a walk ending ends its own follow,
  // never a dry run that has since replaced it).
  function followEnd(why, words, own) {
    var F = follow;
    if (!own) followGen++;
    if (!F || (own && own !== F)) return;
    follow = null;
    clearInterval(uiIv); uiIv = 0;
    if (ui) { ui.remove(); ui = null; }
    try { logEv("follow_end", { why: why, dry: F.dry }); } catch (e) {}
    if (words) try { setErr(words); } catch (e) {}
  }

  // What the creature's screen shows while it follows: an arrow the way it would turn (up is
  // the camera end, left is the robot's left), the skill it would use, and the distances.
  function render(F, c, mode) {
    if (!ui) {
      ui = document.createElement("div");
      ui.id = "gbFollow";
      ui.style.cssText = "position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:99999;" +
        "background:rgba(0,0,0,.72);color:#fff;font:600 15px/1.3 system-ui,sans-serif;padding:8px 14px;" +
        "border-radius:14px;text-align:center;pointer-events:none;white-space:nowrap";
      document.body.appendChild(ui);
    }
    var arrow = c.state === "lost" && !F.track.p ? "\u2026" :
      '<span style="display:inline-block;font-size:30px;transform:rotate(' + (-Math.round(c.heading)) + 'deg)">\u2191</span>';
    var dist = c.dist ? c.dist.toFixed(1) + " m (keep " + c.set.toFixed(1) + ")" : "no one locked";
    var speed = c.speed > 0 ? "come " + c.speed.toFixed(2) : c.speed < 0 ? "back " + (-c.speed).toFixed(2) : "stand";
    ui.innerHTML = (F.dry ? "dry run \u00b7 " : "following \u00b7 ") + arrow + " " + Math.round(c.heading) + "\u00b0<br>" +
      mode + " \u00b7 " + speed + " \u00b7 " + dist;
  }

  function followStart(skills, dry, M) {
    followEnd("replaced");
    var gen = followGen;
    return followLoad().then(function (lib) {
      if (gen !== followGen) return;
      var has = {};
      Object.keys(skills).forEach(function (m) { has[m] = true; });
      var F = follow = {
        lib: lib, dry: dry, M: M || null, cam: camera(lib), heading: 0, t0: Date.now(),
        track: new lib.Track(), f: new lib.Follower(), sel: null, last: null,
      };
      if (dry) {
        return import("/growbot-drive.js").then(function (drive) {
          if (follow !== F) return;
          F.sel = new drive.Selector(has, Date.now());
          uiIv = setInterval(function () {
            if (follow !== F) return;
            var now = Date.now(), c = F.f.command(F.track, 0, now);
            if (!F.track.p && now - F.t0 > LOCK_WAIT_MS) { followEnd("no_face", "I can't see anyone to follow."); return; }
            if (F.track.p && F.track.age(now) > LOST_END_MS) { followEnd("lost", "I lost you, so I stopped."); return; }
            if (now - F.t0 > FOLLOW_MAX_MS) { followEnd("done"); return; }
            render(F, c, F.sel.pick(c, now).mode);
          }, 100);
        });
      }
      uiIv = setInterval(function () {
        if (follow !== F) return;
        if (F.M && _motion !== F.M) { followEnd("walk_ended"); return; }
        if (F.last) render(F, F.last, F.mode || "look");
      }, 100);
    });
  }

  window.gbFollow = {
    // A dry run: everything but the legs, on the skill set's modes.
    dry: function (skills) {
      if (!skills) throw new Error("a dry run needs a skill set");
      if (window.camFacing !== "user" || !window.camOn) throw new Error("open the front camera first: that's what it follows with");
      followStart(skills, true).catch(function (e) { setErr("the follower didn't load (" + ((e && e.message) || e) + ")"); });
      return "dry_run";
    },
    stop: function (why) { followEnd(why || "stopped"); },
    active: function () { return !!follow; },
    // What the creature's next turn is told about a follow (_gbFollowLine in next.html): how
    // long it has run, and what the follower last made of the person ("searching" until it
    // first holds a face, where the follower itself says "lost").
    status: function () {
      var F = follow;
      if (!F || F.dry) return null;
      var c = F.last || {};
      return { secs: Math.round((Date.now() - F.t0) / 1000), state: F.track.p ? String(c.state || "") : "searching",
        dist: typeof c.dist === "number" && isFinite(c.dist) ? c.dist : null,
        heading: typeof c.heading === "number" && isFinite(c.heading) ? c.heading : null };
    },
    closer: function () { return follow ? follow.f.closer() : 0; },
    further: function () { return follow ? follow.f.further() : 0; },
    stay: function (on) { if (follow) follow.f.stay = !!on; },
    // The person stands `metres` away (1 m by default) facing the camera: the nearest face's
    // reading becomes that, on this phone, from now on.
    calibrate: function (metres) {
      var b = seen.boxes, m = +metres || 1;
      if (Date.now() - seen.t > 1500 || !b.length) throw new Error("I can't see a face to measure");
      return followLoad().then(function (lib) {
        var c = camera(lib);
        var raw = Math.min.apply(null, b.map(function (x) { return lib.reading(x, Object.assign({}, c, { k: 1 })).dist; }));
        c.k = lib.calibrate(raw, m);
        saveCamera(c);
        try { logEv("follow_calibrate", { k: Math.round(c.k * 1000) / 1000 }); } catch (e) {}
        return c.k;
      });
    },
    // Which way the frames run: a dry run's arrow pointing away from the person says flip.
    flip: function () {
      return followLoad().then(function (lib) {
        var c = camera(lib);
        c.flipX = !c.flipX;
        saveCamera(c);
        if (follow) follow.cam.flipX = c.flipX;
        return c.flipX;
      });
    },
  };

  /* The body test (growbot-bodytest.js; owner, 2026-11-15: "a button that says body test ... it'll
     tell me what to do ... then it starts the test ... showing telemetry and camera view ... log
     everything so you can review after"). The script and its verdicts live in that module; this
     is its page: the legs (a Drive-steered skill set walk per check, through gbSteer), the
     camera's faces through this phone's calibrated follow camera, the gyro and accelerometer, the
     screen it draws on, and the box it posts its log to. The Body tab starts it as a skill set's
     look with follow "test"; any other move, "stop stop" or the screen's Stop ends it. */
  var bt = null, btLib = null, btUi = null, btVid = null, btLog = null;
  // A start still loading its modules: a second start is refused, and a stop (any move, "stop
  // stop", the Body tab's Stop) bumps btGen so the load never starts a test after it.
  var btLoading = false, btGen = 0;
  function btR(v, k) { k = k || 10; return typeof v === "number" && isFinite(v) ? Math.round(v * k) / k : null; }
  function btFace() {
    if (!followLib || !btLib || !seen.boxes.length || !frontCamera()) return null;
    var big = seen.boxes.reduce(function (a, b) { return !a || b.width > a.width ? b : a; }, null);
    var cam = camera(btLib), r = btLib.reading(big, cam);
    return {
      t: seen.t, bearing: r.bearing, up: r.up, dist: r.dist, side: r.side, n: seen.boxes.length, box: big, src: seen.src,
      score: typeof big.score === "number" ? big.score : null, ms: seen.ms, frame: seen.frame,
    };
  }
  function btTele(drive) {
    var M = typeof _motion !== "undefined" ? _motion : null, out = { kind: M ? String(M.kind || "") : "" };
    if (M && drive && M.drive === drive && M.tele) {
      var w = M.tele;
      out.h = btR(w.h); out.tg = btR(w.tg); out.sp = btR(w.sp, 100); out.p = btR(w.p, 100);
      out.mode = w.mode; out.l = btR(w.l); out.r = btR(w.r);
      // The policy's own side of the tick (see the walker's M.tele): n counts ticks, so a gap
      // in it between samples is ticks the phone ran late.
      out.dt = btR(w.dt); out.tn = w.n;
      if (w.A) out.lev = [btR(w.A.x, 100), btR(w.A.y, 100), btR(w.A.z, 100), btR(w.G.x, 100), btR(w.G.y, 100), btR(w.G.z, 100)];
      if (w.fed) out.fed = [btR(w.fed.fwd, 100), btR(w.fed.yaw, 1000), btR(w.fed.pitch, 100)];
      if (w.act) out.act = [btR(w.act[0], 1000), btR(w.act[1], 1000)];
      if (w.net && w.net !== w.act) out.net = [btR(w.net[0], 1000), btR(w.net[1], 1000)];
      if (w.fs) out.fst = w.fs;
    }
    try {
      if (ori) { out.oa = btR(ori.a); out.ob = btR(ori.b); out.og = btR(ori.g); }
      var a = bodyAccel(), g = bodyGyro();
      out.ax = btR(a.x, 100); out.ay = btR(a.y, 100); out.az = btR(a.z, 100);
      out.gz = btR(g.z / D);
      out.lin = btR(typeof linMag === "number" ? linMag : null, 100);
      out.up = typeof _bodyUpright === "function" ? _bodyUpright() : null;
    } catch (e) {}
    return out;
  }
  function btStill() {
    try {
      var v = btVid && btVid.videoWidth ? btVid : null;
      if (!v) return null;
      var c = document.createElement("canvas");
      c.width = 160; c.height = Math.round(160 * v.videoHeight / v.videoWidth) || 120;
      c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
      var url = c.toDataURL("image/jpeg", 0.6);
      return url.length < 40000 ? url : null;
    } catch (e) { return null; }
  }
  function btEsc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  var BT_CSS = "#gbBT{position:fixed;inset:0;z-index:100000;color:#fff;font:15px/1.35 system-ui,sans-serif;display:flex;flex-direction:column;" +
    "pointer-events:none}#gbBT .pane{pointer-events:auto;background:#000;margin-top:auto;padding:16px 20px 22px;box-sizing:border-box}" +
    "#gbBT.full .pane{margin-top:0;height:100%;overflow:auto;padding:10px 12px 86px}" +
    "#gbBT .dots{display:flex;gap:6px;justify-content:center;margin-bottom:12px}#gbBT .dots i{width:9px;height:9px;border-radius:50%;background:#333}" +
    "#gbBT .dots i.on{background:#fff}#gbBT .dots i.done{background:#8fbc9a}#gbBT .big{font-size:26px;font-weight:700;line-height:1.15;text-align:center;margin:0 0 8px}" +
    "#gbBT .sub{color:#b9bcc2;text-align:center;margin:0 0 14px}#gbBT .say{color:#000;background:#fff;border-radius:10px;padding:0 8px}" +
    "#gbBT button{display:block;width:100%;border:0;border-radius:14px;padding:14px;font:700 16px system-ui,sans-serif;margin-top:8px;background:#fff;color:#000}" +
    "#gbBT button.g{background:#1c1d20;color:#ddd;font-weight:500}#gbBT button.stop{position:fixed;left:12px;right:12px;bottom:12px;width:auto;background:#cf8a8f;font-weight:800}" +
    "#gbBT .st{display:flex;gap:8px;align-items:center;justify-content:center;background:#121315;border-radius:12px;padding:10px;margin-bottom:10px;color:#d6d8dc}" +
    "#gbBT .dot{width:10px;height:10px;border-radius:50%;background:#c9a36a}#gbBT .dot.g{background:#8fbc9a}" +
    "#gbBT .cam{position:relative;width:100%;max-width:420px;margin:0 auto 10px;border-radius:14px;overflow:hidden;background:#1d2025;aspect-ratio:4/3}" +
    "#gbBT .cam video{width:100%;height:100%;object-fit:cover;display:block}#gbBT .fbox{position:absolute;border:2px solid #8fbc9a;border-radius:6px}" +
    "#gbBT .lbl{position:absolute;left:8px;top:6px;font:600 12px ui-monospace,monospace;background:rgba(0,0,0,.6);padding:2px 6px;border-radius:6px}" +
    "#gbBT .rec{position:absolute;right:8px;top:6px;font:600 12px ui-monospace,monospace;background:rgba(207,60,60,.9);padding:2px 6px;border-radius:6px}" +
    "#gbBT .hd{display:flex;justify-content:space-between;align-items:baseline}#gbBT .hd b{font-size:18px}#gbBT .hd span{font:600 13px ui-monospace,monospace;color:#9a9da3}" +
    "#gbBT .ask{background:#fff;color:#000;border-radius:12px;padding:8px 12px;font-weight:700;font-size:18px;text-align:center;margin:8px 0}" +
    "#gbBT .tl{display:grid;grid-template-columns:1fr 1fr;gap:3px 10px;font:12px/1.35 ui-monospace,monospace;color:#cfd2d6;background:#0f1012;border-radius:12px;padding:8px 10px}" +
    "#gbBT .tl i{color:#7d8187;font-style:normal}#gbBT ul{list-style:none;margin:8px 0 0;padding:0;font-size:13px;color:#9a9da3}" +
    "#gbBT li{display:flex;gap:8px;padding:3px 0}#gbBT li b{width:16px;text-align:center}#gbBT li.ok b{color:#8fbc9a}#gbBT li.warn b{color:#c9a36a}" +
    "#gbBT li.fail b{color:#cf8a8f}#gbBT li.now{color:#fff}#gbBT li.now b{color:#c9a36a}#gbBT li em{display:block;font-style:normal;color:#8d9096;font-size:12px}";
  var BT_MARK = { ok: "✓", warn: "!", fail: "✗", now: "●", todo: "○" };

  function btCam(V) {
    var f = V.face, box = f && f.box && Date.now() - f.t < 600 ? f.box : null;
    var lbl = f && Date.now() - f.t < 600 ? (f.bearing >= 0 ? "+" : "") + Math.round(f.bearing) + "° · " + f.dist.toFixed(2) + " m" : "no face";
    return '<div class="cam" data-cam="1">' + (box ? '<div class="fbox" style="left:' + (box.x * 100) + "%;top:" + (box.y * 100) +
      "%;width:" + (box.width * 100) + "%;height:" + (box.height * 100) + '%"></div>' : "") +
      '<div class="lbl">' + btEsc(lbl) + "</div>" + (V.phase === "run" ? '<div class="rec">● REC</div>' : "") + "</div>";
  }
  function btDraw(V) {
    if (!btUi) {
      btUi = document.createElement("div");
      btUi.id = "gbBT";
      var st = document.createElement("style");
      st.textContent = BT_CSS;
      btUi.appendChild(st);
      btUi.appendChild(document.createElement("div")).className = "pane";
      btUi.addEventListener("click", function (ev) {
        var b = ev.target && ev.target.closest ? ev.target.closest("button[data-bt]") : null;
        if (!b) return;
        var what = b.getAttribute("data-bt");
        if (what === "close") { btClose(); return; }
        if (bt) bt.tap(what);
      });
      document.body.appendChild(btUi);
    }
    var pane = btUi.querySelector(".pane"), html = "", t = V.tele || {};
    var full = V.phase !== "setup" || V.setupIdx === 3;
    btUi.className = full ? "full" : "";
    if (V.phase === "setup") {
      var dots = "";
      for (var i = 0; i < 4; i++) dots += "<i class=\"" + (i < V.setupIdx ? "done" : i === V.setupIdx ? "on" : "") + "\"></i>";
      var s = V.setup, title = btEsc(s.title).replace(/“([^”]*)”/, '<span class="say">“$1”</span>');
      if (V.setupIdx === 3) html += btCam(V);
      html += '<div class="dots">' + dots + '</div><p class="big">' + title + '</p><p class="sub">' + btEsc(s.sub) + "</p>";
      if (V.say) html += '<div class="st"><span class="dot"></span>' + btEsc(V.say) + "</div>";
      if (V.setupIdx === 1) html += '<div class="st"><span class="dot' + (V.bodyUp ? " g" : "") + '"></span>' + (V.bodyUp ? "Legs connected" : "Waiting for the legs…") + "</div>";
      if (V.setupIdx === 3) {
        var seenNow = V.face && Date.now() - V.face.t < 600;
        html += '<div class="st"><span class="dot' + (seenNow ? " g" : "") + '"></span>' + (seenNow ? "I can see you · listening" : "I can't see a face yet") + "</div>";
      }
      if (V.waiting && V.waiting.indexOf("tap") >= 0) {
        html += '<button data-bt="tap">' + (V.setupIdx === 0 ? "It's on" : V.setupIdx === 2 ? "Stand up" : V.setupIdx === 3 ? "Measure now" : "Next") + "</button>";
      }
      html += '<button class="g" data-bt="cancel">Cancel test</button>';
    } else if (V.phase === "run") {
      var c = V.checks[V.checkIdx] || { name: "" }, mm = Math.floor(V.secs / 60), ss = V.secs % 60;
      html += '<div class="hd"><b>' + (V.checkIdx + 1) + " / " + V.checks.length + " · " + btEsc(c.name) + "</b><span>" +
        (mm < 10 ? "0" : "") + mm + ":" + (ss < 10 ? "0" : "") + ss + "</span></div>";
      if (V.ask) html += '<div class="ask">' + btEsc(V.ask) + "</div>";
      html += btCam(V);
      var row = function (k, v) { return "<div><i>" + k + " </i>" + btEsc(v === null || v === undefined ? "–" : v) + "</div>"; };
      html += '<div class="tl">' + row("skill", t.mode || t.kind || "still") +
        row("cmd", t.tg !== undefined ? "h" + (t.tg >= 0 ? "+" : "") + t.tg + " s" + t.sp + " p" + t.p : null) +
        row("heading", t.h !== undefined ? t.h + "°" : null) + row("tilt", t.ob !== undefined ? "b" + t.ob + " g" + t.og : null) +
        row("legs", t.l !== undefined ? "L " + t.l + " R " + t.r : null) + row("gyro z", t.gz !== null ? t.gz + "°/s" : null) +
        row("upright", t.up === null ? "?" : t.up ? "yes" : "NO") + row("sent", V.rowsSent + " chunks") + "</div>";
      html += "<ul>" + V.checks.map(function (k) {
        return '<li class="' + k.state + '"><b>' + BT_MARK[k.state] + "</b><span>" + btEsc(k.name) + "</span></li>";
      }).join("") + "</ul>";
      html += '<button class="stop" data-bt="cancel">STOP · or say “stop stop”</button>';
    } else {
      var words = { done: "Body test done", stopped: "Body test stopped", failed: "Body test hit an error" }[V.status] || "Body test";
      html += '<p class="big" style="text-align:left">' + words + '</p><p class="sub" style="text-align:left">' +
        Math.floor(V.secs / 60) + " min " + (V.secs % 60) + " s · saved on the box as " + btEsc(V.run) + "</p><ul>" +
        V.checks.concat(V.falls ? [{ name: "Falls", state: V.falls.v, why: V.falls.why }] : []).map(function (k) {
          return '<li class="' + k.state + '"><b>' + BT_MARK[k.state] + "</b><span>" + btEsc(k.name) + (k.why ? "<em>" + btEsc(k.why) + "</em>" : "") + "</span></li>";
        }).join("") + '</ul><button data-bt="close">Done</button>';
    }
    // The camera's video element is kept across redraws, so the picture never blinks.
    if (btVid && btVid.parentNode) btVid.parentNode.removeChild(btVid);
    pane.innerHTML = html;
    var slot = pane.querySelector("[data-cam]");
    if (slot) {
      var stream = typeof camStream !== "undefined" ? camStream : null;
      if (!btVid) {
        btVid = document.createElement("video");
        btVid.muted = true; btVid.setAttribute("playsinline", ""); btVid.setAttribute("autoplay", "");
      }
      if (stream && btVid.srcObject !== stream) { btVid.srcObject = stream; try { btVid.play(); } catch (e) {} }
      slot.insertBefore(btVid, slot.firstChild);
    }
  }
  function btClose() {
    if (bt && bt.phase !== "result") return;
    bt = null;
    btUnhook();
    if (btUi) { btUi.remove(); btUi = null; }
    if (btVid) { btVid.srcObject = null; btVid = null; }
  }
  function btUnhook() {
    if (btLog && window.logEv === btLog.mine) window.logEv = btLog.real;
    btLog = null;
  }

  window.gbBodyTest = {
    start: function (skills) {
      if (!skills) throw new Error("a body test needs a skill set");
      if (btLoading || (bt && bt.phase !== "result")) throw new Error("a body test is already running");
      btClose();
      btLoading = true;
      var gen = ++btGen;
      // A pause kept from before (it survives a reload, and the wake tap keeps it) refuses every
      // move, the get-up included: 2026-11-16 runs #1 and #26 stood at "stand up" behind one.
      // Starting a body test is asking it to move.
      try {
        if (typeof paused !== "undefined" && paused && typeof setPaused === "function") {
          setPaused(false);
          logEv("bodytest_unpause", {});
        }
      } catch (x) {}
      try { ensureIMU(); } catch (x) {}
      // Whatever the legs or the follower were doing stops first.
      window.gbFollow.stop("new_move");
      if (typeof _motion !== "undefined" && _motion) stopMotion();
      Promise.all([import("/growbot-bodytest.js"), followLoad()]).then(function (mods) {
        btLoading = false;
        if (gen !== btGen) return;
        var mod = mods[0];
        btLib = mods[1];
        var drawn = 0, env = {
          now: function () { return Date.now(); },
          sleep: function (ms) { return new Promise(function (ok) { setTimeout(ok, ms); }); },
          newId: function () { return "bt-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8); },
          newDrive: function () { var s = ""; while (s.length < 16) s += Math.random().toString(16).slice(2); return s.slice(0, 16); },
          show: function (V) { var n = Date.now(); if (V.phase === "run" && n - drawn < 200) return; drawn = n; btDraw(V); },
          bodyUp: function () { return !!(window._body && window._body.ready && _bodyAlive(true)); },
          upright: function () { return typeof _bodyUpright === "function" ? _bodyUpright() : null; },
          // "getup" (upstream's get-up from lying screen-up, its legs under way), "tall" (the
          // tall stance, already up), or "" when it isn't known to be up and the get-up didn't
          // start: the tall stance from flat only pushes the legs into the floor.
          standUp: function () {
            try { ensureIMU(); } catch (x) {}
            if (_bodyUpright() !== true) {
              if (typeof window.gbGetUp !== "function") return "";
              window.gbGetUp();
              var a = window._moveLast;
              return a && a.action === "recover_screen_up" && a.state === "started" ? "getup" : "";
            }
            var t = typeof STAND_TALL !== "undefined" && STAND_TALL ? STAND_TALL : { l: 50, r: 130, ms: 600 };
            window.moveLegs({ body: { steps: [{ l: t.l, r: t.r, ms: t.ms }] } });
            return "tall";
          },
          camFront: function () {
            try {
              if (window.camFacing !== "user" && typeof gbEyeTo === "function") gbEyeTo("user", "bodytest");
              if (!window.camOn && typeof ensureCam === "function") ensureCam(false);
              // A body test is a phone back on its body: say why its camera stays shut.
              if (window.TRAVEL && typeof toast === "function") {
                toast("travel mode is on, so the camera stays shut. turn it off in settings first.", 5000);
              }
            } catch (e) {}
            return frontCamera();
          },
          face: btFace,
          tele: function () { return btTele(bt ? bt.drive : ""); },
          calibrate: function () { return window.gbFollow.calibrate(1); },
          flip: function () { return window.gbFollow.flip(); },
          still: btStill,
          walk: function (sk, drive) {
            window.gbTrainedWalk(sk, "look", 15, drive);
            var M = typeof _motion !== "undefined" ? _motion : null;
            if (M && M.drive === drive) return "";
            var a = window._moveLast;
            return String((a && a.reason) || "not admitted");
          },
          steer: function (drive, h, sp, p) {
            return !!(window.gbSteer && window.gbSteer({ drive: drive, heading: h, speed: sp, pitch: p }));
          },
          legsOff: function () {
            var M = typeof _motion !== "undefined" ? _motion : null;
            if (!M || !bt || M.drive !== bt.drive) return;
            _imuWhy = "done";
            carryLegMotion({ l: 90, r: 90 });
          },
          post: function (chunk) {
            chunk.robot = btRobot();
            return fetch("/api/box/bodytest", {
              method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(chunk),
            });
          },
        };
        var run = bt = new mod.BodyTest(env, skills, { ua: String(navigator.userAgent || "").slice(0, 120) });
        // Every diary row the page logs while it runs (walks, skills, falls, stops) joins its log.
        btUnhook();
        if (typeof window.logEv === "function") {
          var real = window.logEv, mine = function (e, d) {
            try { if (bt === run) run.event(e, d); } catch (x) {}
            return real.apply(this, arguments);
          };
          btLog = { real: real, mine: mine };
          window.logEv = mine;
        }
        try { logEv("bodytest_start", { run: run.run_id }); } catch (e) {}
        return run.run().then(function () { if (bt === run) btUnhook(); });
      }).catch(function (e) {
        btLoading = false;
        btUnhook();
        setErr("the body test didn't load (" + ((e && e.message) || e) + ")");
      });
      return "body_test";
    },
    stop: function (why) { btGen++; if (bt) bt.cancel(why || "stopped"); },
    active: function () { return btLoading || (!!bt && bt.phase !== "result"); },
    // A transcript: true when the test took it, so no model turn hears it.
    heard: function (txt) { return !!(bt && bt.phase !== "result" && bt.heard(txt)); },
  };

  // The wake screen's camera test (owner, 2026-11-15: "set up the phone and you do face
  // tracking on me and make sure that it can keep up"): a plain link, since the test is its own
  // page (camtest.html) that holds the camera itself, with no legs and no creature running.
  function ctWakeButton() {
    if (typeof document.getElementById !== "function") return false;
    var wake = document.getElementById("wgWake");
    if (!wake || !wake.parentNode || document.getElementById("wgCamTest")) return false;
    var id = btRobot(), b = document.createElement("button");
    b.id = "wgCamTest";
    b.type = "button";
    b.textContent = "camera test";
    b.style.cssText = "font:13px/1 GBEntryMono,'Departure Mono',ui-monospace,monospace;border:2px solid #7F93AB;border-radius:0;background:transparent;color:#E8F6FF;padding:12px 16px;min-height:44px;min-width:240px;margin-top:14px;cursor:pointer";
    b.onclick = function () { location.href = "/camtest.html" + (id ? "?id=" + encodeURIComponent(id) : ""); };
    var after = document.getElementById("wgBodyTest") || wake;
    after.parentNode.insertBefore(b, after.nextSibling);
    return true;
  }
  try {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ctWakeButton);
    else ctWakeButton();
  } catch (e) {}

  // The robot this phone wears, as the cal poll reads it: the page's ?id=, else the last one.
  function btRobot() {
    var id = "";
    try { id = new URLSearchParams(location.search).get("id") || window.localStorage.getItem("gb_walk_id") || ""; } catch (e) {}
    id = String(id).trim();
    return /^gb-[A-Za-z0-9]{1,32}$/.test(id) ? id : "";
  }

  // The wake screen's Body test (owner, 2026-11-16: "I only have the one phone"): on a robot's
  // phone, a button under Wake that wakes the creature and starts the test with the library's
  // skill set, so the phone that sits on the body never needs the PWA in hand. The wake tap
  // runs first and in the tap itself, since iOS only unlocks the voice and the motion sensors
  // inside a gesture; the skill set is read after.
  function btWakeButton() {
    var id = btRobot();
    if (!id || typeof document.getElementById !== "function") return false;
    var wake = document.getElementById("wgWake");
    if (!wake || !wake.parentNode || document.getElementById("wgBodyTest")) return false;
    var b = document.createElement("button");
    b.id = "wgBodyTest";
    b.type = "button";
    b.textContent = "body test";
    b.style.cssText = "font:13px/1 GBEntryMono,'Departure Mono',ui-monospace,monospace;border:2px solid #7F93AB;border-radius:0;background:transparent;color:#E8F6FF;padding:12px 16px;min-height:44px;min-width:240px;margin-top:14px;cursor:pointer";
    b.onclick = function () {
      wake.click();
      fetch("/api/box/skills?robot=" + encodeURIComponent(id), { cache: "no-store" }).then(function (r) {
        if (r.status === 403) throw new Error("this phone isn't signed in to drive " + id);
        if (!r.ok) throw new Error("the box said " + r.status);
        return r.json();
      }).then(function (j) {
        if (!j || !j.skills) throw new Error("no trained walks cover two skills yet");
        window.gbBodyTest.start(j.skills);
      }).catch(function (e) {
        setErr("the body test didn't start (" + ((e && e.message) || e) + ")");
      });
    };
    wake.parentNode.insertBefore(b, wake.nextSibling);
    return true;
  }
  try {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", btWakeButton);
    else btWakeButton();
  } catch (e) {}

  function load(pid) {
    var ref = "/api/box/policy?id=" + encodeURIComponent(pid), hit = cache[pid];
    if (!hit || Date.now() - hit.t >= KEEP_MS) {
      hit = cache[pid] = { t: Date.now(), p: fetch(ref, { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error(r.status === 404 ? "that policy isn't on the box any more" : "policy " + r.status);
        return r.json();
      }).catch(function (e) { delete cache[pid]; throw e; }) };
    }
    return hit.p;
  }

  function channels(pj) {
    var sp = (pj && pj.obs_spec) || {}, out = [];
    (Array.isArray(sp.segments) ? sp.segments : [sp]).forEach(function (seg) {
      if (seg && Array.isArray(seg.channels)) out = out.concat(seg.channels);
    });
    return out;
  }

  // What the pad's move asks of a heading policy: a target (deg, counter-clockwise from where
  // the facing end points at the start) and a speed.
  function command(move, hasSpeed) {
    // A trick: hold it facing ahead, in place when it takes a speed; a heading-only one walks.
    if (move === "look") return { target: 0, fwd: hasSpeed ? 0 : 1 };
    if (move === "spin_left") return { target: 90, fwd: hasSpeed ? 0 : 1 };
    if (move === "spin_right") return { target: -90, fwd: hasSpeed ? 0 : 1 };
    if (move === "back") return hasSpeed ? { target: 0, fwd: -1 } : { target: 180, fwd: 1, around: true };
    return { target: 0, fwd: 1 };
  }

  function wrap180(d) { d = ((d + 180) % 360 + 360) % 360 - 180; return d; }

  function mul(R, v) {
    return { x: R[0][0] * v.x + R[0][1] * v.y + R[0][2] * v.z,
             y: R[1][0] * v.x + R[1][1] * v.y + R[1][2] * v.z,
             z: R[2][0] * v.x + R[2][1] * v.y + R[2][2] * v.z };
  }

  // The smallest rotation taking unit vector u onto +z (Rodrigues): v = u x z, c = u . z.
  function levelling(u) {
    var n = Math.hypot(u.x, u.y, u.z);
    if (!(n > 1)) return null;
    var a = u.x / n, b = u.y / n, c = u.z / n;
    if (c < -0.999999) return [[1, 0, 0], [0, -1, 0], [0, 0, -1]];
    var vx = b, vy = -a, k = 1 / (1 + c);
    var V = [[0, 0, vy], [0, 0, -vx], [-vy, vx, 0]], R = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    for (var i = 0; i < 3; i++) for (var j = 0; j < 3; j++) {
      var v2 = 0;
      for (var m = 0; m < 3; m++) v2 += V[i][m] * V[m][j];
      R[i][j] += V[i][j] + v2 * k;
    }
    return R;
  }

  function bodyAccel() { var a = window.accel || accel; return { x: a.y || 0, y: -(a.x || 0), z: a.z || 0 }; }
  function bodyGyro() { var r = window.rate || rate; return { x: (r.b || 0) * D, y: -(r.a || 0) * D, z: (r.g || 0) * D }; }

  function refuse(attempt, why, words) {
    _moveReport(attempt, "blocked", why);
    setErr(words);
  }

  // A skill set's command for the pad's move, in the front camera's frame (growbot-drive.js):
  // the target heading (deg, counter-clockwise from where the camera points at the start) and a
  // signed speed. Back backs away still facing ahead, never turning round (owner, 2026-11-15).
  function setCommand(move) {
    if (move === "forward") return { target: 0, speed: 1 };
    if (move === "back") return { target: 0, speed: -1 };
    if (move === "spin_left") return { target: 90, speed: 0 };
    if (move === "spin_right") return { target: -90, speed: 0 };
    return { target: 0, speed: 0 };
  }

  // A skill set's distinct files, loaded once each: {mode: pid} -> {mode: {pid, pj}}.
  function loadSet(skills) {
    var modes = Object.keys(skills), pids = [];
    modes.forEach(function (m) { if (pids.indexOf(skills[m]) < 0) pids.push(skills[m]); });
    return Promise.all(pids.map(load)).then(function (got) {
      var out = {};
      modes.forEach(function (m) { out[m] = { pid: skills[m], pj: got[pids.indexOf(skills[m])] }; });
      return out;
    });
  }

  function start(pid, move, secs, attempt, drive, following, own) {
    // `pid` is one library policy, or a skill set {look?, forward?, back?} of them.
    var skills = pid && typeof pid === "object" ? pid : null;
    if (following && !skills) { refuse(attempt, "no_skills", "following needs a skill set"); return; }
    if (following && !frontCamera()) { refuse(attempt, "no_front_camera", "open the front camera first: that's what it follows with"); return; }
    var label = skills ? "set:" + Object.keys(skills).join("+") : pid;
    if (_wheels) { refuse(attempt, "no_legs", "a trained walk needs legs, and this body rolls"); return; }
    if (!window._body || !window._body.ready) { refuse(attempt, "connection_unavailable", "no robot connected to walk — its legs aren't here"); return; }
    if (!_bodyAlive()) { refuse(attempt, "connection_unavailable", "robot not answering"); return; }
    ensureIMU();
    if (!ori) { refuse(attempt, "no_orientation", "no orientation readings from this phone — tap to allow motion, then try again"); return; }
    if (Date.now() < _gaitCoolUntil) { refuse(attempt, "cooling", "catching my balance a moment…"); return; }
    var left = dutyLeftMs();
    if (left < 800) { refuse(attempt, "duty_limit", "resting its legs a moment (duty limit)"); return; }
    var dflt = move === "forward" || move === "back" ? 8 : 4, walkS = policySeconds(secs, dflt, left);
    carryLegMotion();
    var gen = _motionGen, M = { kind: "walk", loading: true, report: attempt, drive: following ? "" : drive || "" };
    // Dave's own walk (gbOwnWalk): another ask for the same walk while this one runs carries
    // it on (M.extend) instead of a new stance glide, and it stops itself when he stops looking.
    if (own) M.own = move;
    // Until the stride starts, a longer ask is a longer walk; once it runs, extend() below.
    // Each returns the walk's whole length in seconds when it grew, else 0.
    M.extend = function (s) { if (s <= walkS) return 0; walkS = s; return walkS; };
    _motion = M;
    var fol = null;
    _moveReport(attempt, "accepted", "loading_policy");
    $("brainTxt").textContent = "loading my trained walk…";
    var policy = null, files = null, lib = null;
    (skills ? loadSet(skills) : load(pid)).then(function (got) {
      // Replaced while its files loaded: never start a follower that would end the new one's.
      if (gen !== _motionGen || _motion !== M) return [null, null, null];
      if (skills) files = got; else policy = got;
      return Promise.all([import("/growbot_runner.js"), skills ? import("/growbot-drive.js") : null,
        following ? followStart(skills, false, M) : null]);
    }).then(function (mods) {
      if (following) fol = follow && follow.M === M ? follow : null;
      if (following && !fol && _motion === M) { stopMotion(); return; }
      var mod = mods[0];
      lib = mods[1];
      if (gen !== _motionGen || _motion !== M) return;
      if (paused || document.hidden || dreaming) { stopMotion(); return; }
      var cal = Object.assign({}, calNow(), { IMU_SIGN: [1, 1, 1], gain: 1, turn: 0 });
      var walker = null, hz = 25, set = null, sel = null, mode = "look", settleEnd = 0;
      function walkerFor(pj) {
        var w = new mod.Walker(pj, cal);
        w.setFrame("zup");
        // The robot's leg ports are named from the other end from the sim's legs, so its action
        // 0 ("right_leg", seen from the nose) drives the left port. Unswapped, every turn ran the
        // wrong way and holding a heading spun away from it: body test #40 drifted 15 deg/s
        // counter-clockwise at a target of 0 and turned to -20 when told +90, and the sim with
        // the legs swapped does the same, while normal it holds (GrowBot_Training, 2026-11-16).
        w.setSwap(!w.swap);
        return w;
      }
      if (skills) {
        // One Walker per distinct file; a master policy serving every mode is one Walker.
        var made = {}, has = {};
        set = {};
        Object.keys(files).forEach(function (m) {
          var f = files[m];
          if (!made[f.pid]) made[f.pid] = { w: walkerFor(f.pj), prof: lib.profile(f.pj) };
          set[m] = made[f.pid];
          has[m] = true;
          hz = Number((f.pj.obs_spec || {}).control_hz) || hz;
        });
        sel = new lib.Selector(has, Date.now());
      } else {
        var sp = policy.obs_spec || {}, yaw = sp.cmd_yaw || {};
        if (yaw.meaning !== "heading_error") throw new Error("that policy isn't steered by a heading");
        walker = walkerFor(policy);
        hz = Number(sp.control_hz) || 25;
      }
      var hasSpeed = !skills && channels(policy).indexOf("cmd_forward") >= 0;
      var hasPitch = !skills && channels(policy).indexOf("cmd_pitch") >= 0;
      var cmd = skills ? setCommand(move) : command(move, hasSpeed), pitch = 1, walkT = 0;
      var L = +cal.L_OFF || 0, Rr = +cal.R_OFF || 0;
      var prepEnd = Date.now() + PREP_MS, grav = { x: 0, y: 0, z: 0, n: 0 };
      var R = null, heading = 0, lastT = 0, endT = 0, upB = null, tipN = 0, jerkN = 0;
      // At least `s` more seconds from now; an ask within what is left changes nothing (only a
      // stop ever shortens a walk).
      M.extend = function (s) {
        if (!endT) { if (s <= walkS) return 0; walkS = s; return walkS; }
        var want = Date.now() + s * 1000;
        if (want <= endT) return 0;
        _walkLog.push({ t: Date.now(), ms: want - endT });
        endT = want;
        selfMoveUntil = Math.max(selfMoveUntil, endT + 3500);
        return Math.round((endT - walkT) / 100) / 10;
      };
      M.loading = false;
      _walkLog.push({ t: Date.now(), ms: walkS * 1000 });
      selfMoveUntil = Date.now() + PREP_MS + walkS * 1000 + 3500;
      $("brainTxt").textContent = "finding my stance…";

      function gone() {
        if (_motion !== M) return true;
        if (paused || document.hidden || dreaming || !window._body || !window._body.ready || !_bodyAlive()) { stopMotion(); return true; }
        return false;
      }
      function send(l, r) {
        holdStop(false);
        (window._body.raw || window._body).pose(l, r);
        M.lastPose = { l: l - L, r: r - Rr };
      }
      // A tick runs off a timer, outside the load's catch: a throw there must still free the legs.
      function guarded(tick) {
        return function () {
          try { tick(); } catch (e) {
            if (_motion !== M) return;
            stopMotion();
            setErr("my trained walk stumbled (" + ((e && e.message) || e) + ")");
            $("brainTxt").textContent = "awake";
          }
        };
      }
      var prep = guarded(function () {
        if (gone()) return;
        try { send(90 + L, 90 + Rr); } catch (e) { stopMotion(); return; }
        if (!M.started) { M.started = true; _moveReport(attempt, "started", "stance_commands", { gait: "trained", policy: label }); }
        // The stance's gravity, from its last 400 ms once the legs have settled.
        if (prepEnd - Date.now() <= 400) { var a = bodyAccel(); grav.x += a.x; grav.y += a.y; grav.z += a.z; grav.n++; }
        if (Date.now() < prepEnd) { M.loopIv = setTimeout(prep, 100); return; }
        R = grav.n ? levelling({ x: grav.x / grav.n, y: grav.y / grav.n, z: grav.z / grav.n }) : null;
        if (!R) { stopMotion(); setErr("couldn't read which way is up — is motion allowed?"); return; }
        upB = { b: ori.b, g: ori.g };
        if (walker) walker.reset();
        lastT = performance.now(); walkT = Date.now(); endT = walkT + walkS * 1000;
        // Dave's own walk (gbOwnWalk) is timed from here, as runGait's are: "ran X s", and an
        // early end trims the self-motion window so a fall after it is felt.
        if (attempt && attempt.mv) attempt.mv.t0 = walkT;
        try { logEv("walk_start", { gait: "trained", policy: label, dir: move, target: cmd.target, fwd: skills ? cmd.speed : cmd.fwd, granted: walkS }); } catch (e) {}
        $("brainTxt").textContent = "walking…";
        step();
      });
      function endFollowWalk(why, words) {
        // "done", so carryLegMotion settles to its rest stance rather than holding a stride.
        _imuWhy = "done";
        carryLegMotion({ l: 90, r: 90 });
        // ...but the move itself didn't finish: it ended for this reason.
        _moveReport(attempt, "cancelled", why);
        followEnd(why, words, fol);
        try { logEv("walk_end", { gait: "trained", policy: label, why: why, heading: Math.round(heading / D) }); } catch (e) {}
        $("brainTxt").textContent = "awake";
      }
      var step = guarded(function () {
        if (gone()) return;
        var t0 = performance.now(), live = M.drive && steer && steer.drive === M.drive ? steer : null;
        if (fol) {
          // The follower steers, and keeps the walk going while it has someone (or briefly lost).
          var fnow = Date.now(), fc = fol.f.command(fol.track, heading / D, fnow);
          if (!fol.track.p && fnow - walkT > LOCK_WAIT_MS) return endFollowWalk("no_face", "I can't see anyone to follow.");
          if (fol.track.p && fol.track.age(fnow) > LOST_END_MS) return endFollowWalk("lost", "I lost you, so I stopped.");
          cmd = { target: wrap180(heading / D + fc.heading), speed: fc.speed };
          pitch = fc.pitch;
          // Each change of the follower's state, with where the face was across the frame:
          // a "lost" row's side is how far round the camera sees before it loses someone, and
          // past is the extra turn the search takes beyond it.
          // At most one a second: hold and follow can flip every tick at a turn limit, and the
          // diary keeps only 240 rows.
          if (fol.track.p && (!fol.last || fol.last.state !== fc.state) &&
              (fc.state === "lost" || fnow - (fol.stateLogT || 0) >= 1000)) {
            fol.stateLogT = fnow;
            var fp = fol.track.p;
            try {
              logEv("follow_state", {
                from: fol.last ? fol.last.state : "", to: fc.state, side: Math.round((+fp.side || 0) * 100) / 100,
                dist: Math.round((+fp.dist || 0) * 100) / 100, err: Math.round(fc.heading), sp: fc.speed, past: fc.past || 0,
                age: Math.round(fnow - fp.t),
              });
            } catch (e) {}
          }
          fol.last = fc;
          var fwant = Math.min(fnow + STEER_HOLD_MS, walkT + FOLLOW_MAX_MS);
          if (fwant > endT && dutyLeftMs() >= fwant - endT) {
            _walkLog.push({ t: fnow, ms: fwant - endT });
            endT = fwant;
            selfMoveUntil = Math.max(selfMoveUntil, endT + 3500);
          }
        } else if (live) {
          // At exactly ±180 the error's sign would flip with the noise, as for the pad's back.
          if (skills) {
            cmd = { target: live.heading, speed: live.speed, around: Math.abs(live.heading) === 180 };
            pitch = live.pitch;
          }
          else {
            cmd = { target: live.heading, fwd: hasSpeed ? live.speed : 1, around: Math.abs(live.heading) === 180 };
            pitch = hasPitch ? live.pitch : 1;
          }
          var want = Math.min(live.t + STEER_HOLD_MS, walkT + DRIVE_MAX_MS);
          if (want > endT && dutyLeftMs() >= want - endT) {
            _walkLog.push({ t: Date.now(), ms: want - endT });
            endT = want;
            selfMoveUntil = Math.max(selfMoveUntil, endT + 3500);
          }
        }
        var noLook = M.own && ownNoLook(walkT);
        if (Date.now() >= endT || noLook) {
          _imuWhy = "done";
          carryLegMotion({ l: 90, r: 90 });
          try { logEv("walk_end", { gait: "trained", policy: label, why: noLook ? "no_look" : "done", heading: Math.round(heading / D) }); } catch (e) {}
          $("brainTxt").textContent = "awake";
          return;
        }
        // Upstream's tip and grab abort (runGait's LEGS-3), on the same numbers.
        var dTilt = ori && upB ? phoneTiltDegrees(ori, upB) : null, lin = typeof linMag === "number" ? linMag : 0;
        tipN = dTilt > 55 ? tipN + 1 : 0;
        jerkN = lin > 11 ? jerkN + 1 : 0;
        if (tipN >= 5 || jerkN >= 6) {
          var why = tipN >= 5 ? "tipped" : "grabbed";
          _imuWhy = why; stopMotion(); _gaitCoolUntil = Date.now() + 4000;
          try { logEv("gait_abort", { why: why, gait: "trained", policy: label }); } catch (e) {}
          setErr(why === "tipped" ? "I tipped over, so I stopped." : "easy — set me down gently.");
          $("brainTxt").textContent = "awake";
          return;
        }
        var A = mul(R, bodyAccel()), G = mul(R, bodyGyro());
        var tickMs = t0 - lastT, dt = Math.min(0.1, Math.max(0, tickMs / 1000));
        lastT = t0;
        heading += G.z * dt;
        // The gyro carries the heading between camera frames: readings land in the walk's frame.
        if (fol) fol.heading = heading / D;
        var err = wrap180(cmd.target - heading / D), out, fed = null;
        if (skills) {
          // Just under 180: inputs() wraps again, and +180 would wrap to -180, a right turn.
          if (cmd.around && Math.abs(err) > 90) err = Math.min(Math.abs(err), 179.9);
          // The heading is the gyro's, the same whichever end leads, so err is the front
          // camera's error for every skill (growbot-drive.js, THE FRAME).
          var c = { heading: err, speed: cmd.speed, pitch: pitch }, pick = sel.pick(c, Date.now());
          if (pick.changed) {
            var was = set[mode] || null, next = set[pick.mode] || null;
            try { logEv("skill", { from: mode, to: pick.mode }); } catch (e) {}
            mode = pick.mode;
            if (fol) fol.mode = mode;
            // Another file takes over from a still stance with a fresh history, as it trained.
            if (next && next !== was) { settleEnd = Date.now() + SETTLE_MS; next.w.reset(); }
          }
          var sk = set[mode] || null;
          if (!sk || Date.now() < settleEnd) out = { l: 90 + L, r: 90 + Rr };
          else {
            var inp = lib.inputs(sk.prof, mode, c);
            fed = inp;
            out = sk.w.step(A, G, { fwd: inp.fwd, yaw: inp.yaw, pitch: inp.pitch });
          }
        } else {
          if (cmd.around && Math.abs(err) > 90) err = Math.abs(err);
          fed = { fwd: cmd.fwd, yaw: err / 180, pitch: pitch };
          out = walker.step(A, G, fed);
        }
        try { send(out.l, out.r); } catch (e) { stopMotion(); return; }
        // What the body test samples: the walk's own heading, command and leg poses.
        // And what the policy was given and gave back, for comparing a run with the sim: the
        // tick's real interval (it trained at a fixed rate), the levelled accelerometer and
        // gyro it read, the command inputs, and its actions before the legs' calibration (act, as
        // sent; net, before the policy's declared low-pass).
        M.tele = {
          h: heading / D, tg: cmd.target, sp: skills ? cmd.speed : cmd.fwd, p: pitch, mode: skills ? mode : "", l: out.l, r: out.r,
          dt: tickMs, n: (M.tele ? M.tele.n : 0) + 1, A: A, G: G, fed: fed, act: out.action || null, net: out.raw || null,
          fs: fol && fol.last ? fol.last.state : "",
        };
        // Each file ticks at the rate it trained at: its clock and history count steps.
        var rate = skills && set[mode] ? set[mode].prof.hz : hz;
        M.loopIv = setTimeout(step, Math.max(0, 1000 / rate - (performance.now() - t0)));
      });
      prep();
    }).catch(function (e) {
      if (gen !== _motionGen) return;
      stopMotion();
      setErr("trained walk didn't load (" + ((e && e.message) || e) + ")");
      $("brainTxt").textContent = "awake";
    });
  }

  // As runGait: one move request, and a move it never admitted reads as blocked.
  window.gbTrainedWalk = function (pid, move, secs, drive, following) {
    var attempt = _moveRequest((following ? "walk:follow:" : "walk:trained:") + String(move).slice(0, 16));
    try { start(pid, move, +secs || 0, attempt, drive, !!following); }
    finally { if (attempt.state === "requested") _moveReport(attempt, "blocked", "not_admitted"); }
  };

  // Dave's own forward and back walks can take the box's trained skills (Dave simulator,
  // 2026-11-17): in the sim, on the robot's wiring as modelled, the trained forward walked
  // him to the person where the stock one barely moved him (and "come here" fell short). On
  // by default, as the trained follow already is on the same wiring; ?ownwalk=0 turns it off
  // in a tab. runGait can't wait on a fetch, so the set is read ahead. The trained walk
  // takes no stride gain, and ?diygait and a seek's duty defer keep their lanes.
  var ownSet = null, ownT = 0, OWN_MS = 60000, OWN_ID = /^[A-Za-z0-9-]{1,64}$/;
  var stockOnly = false;
  try { stockOnly = new URLSearchParams(location.search).get("ownwalk") === "0"; } catch (e) {}
  function readOwnSet() {
    var id = btRobot();
    if (!id || stockOnly || document.hidden || typeof fetch !== "function") return;
    ownT = Date.now();
    fetch("/api/box/skills?robot=" + encodeURIComponent(id), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : undefined; })
      .then(function (j) {
        // A failed read (the box restarting after an update) keeps the last good set.
        if (j === undefined) return;
        var v = j && j.skills, out = null;
        if (v && typeof v === "object" && !Array.isArray(v)) {
          ["look", "forward", "back"].forEach(function (k) {
            if (typeof v[k] === "string" && OWN_ID.test(v[k])) { out = out || {}; out[k] = v[k]; }
          });
        }
        ownSet = out;
      })
      .catch(function () {});
  }
  // Read at load and on each return to the page, and again behind a walk once a minute old:
  // no timer, so a page left open polls nothing.
  try {
    readOwnSet();
    document.addEventListener("visibilitychange", readOwnSet);
  } catch (e) {}
  // True when it took the walk: `attempt` is runGait's, so the move's history reads as the
  // creature's own walk, marked trained.
  // A long walk is only safe while he keeps looking (upstream's LONG WALK dead-man, 11-16): with
  // no thought issued or answered for OWN_LOOK_MS since the stride began he stops, as he does
  // when the brain can't answer. A thought still in flight is a look, for twice as long: the
  // box's model can take tens of seconds over one with pictures.
  var OWN_LOOK_MS = 30000;
  function ownNoLook(t0) {
    var fl = typeof inFlight !== "undefined" && inFlight && !(typeof dreaming !== "undefined" && dreaming);
    var last = Math.max(t0, typeof lastCallT === "number" ? lastCallT : 0, typeof lastReplyT === "number" ? lastReplyT : 0);
    return !!t0 && Date.now() - last >= OWN_LOOK_MS * (fl ? 2 : 1);
  }
  // For the page's walk look (apply.py EXPLORE_PATCHES): his own walk is under way.
  window.gbOwnWalking = function () { return !!(_motion && _motion.own); };
  window.gbOwnWalk = function (spec, dir, attempt) {
    if (Date.now() - ownT > OWN_MS) { try { readOwnSet(); } catch (e) {} }
    // The model reads its walks back as "walk fwd (trained)" and may echo the gait. runGait has
    // no "trained" gait and falls back to the screen-backward official one, so a walk this
    // turns down must reach it as a plain walk, not walk away from the person.
    if (spec && spec.gait === "trained") delete spec.gait;
    if (stockOnly || !ownSet || _wheels || (spec && spec.stock)) return false;
    if (typeof _diyGaitFlag === "function" && _diyGaitFlag()) return false;
    var gait = String((spec && spec.gait) || "");
    if (gait && !/^(official|screen_forward|screen_backward)$/.test(gait)) return false;
    var back = /^(back|backward|backwards|screen-backward)$/.test(dir) || gait === "screen_backward";
    var fwd = !back && /^(fwd|forward|ahead|go|straight|walk|closer)$/.test(dir);
    var move = back ? "back" : fwd ? "forward" : "";
    if (!move || !ownSet[move]) return false;
    // The same walk, asked again while it runs (he sends walk each look to keep going): carry
    // it on, no new stance. The ask itself is reported merged; the running walk stays his
    // latest move, its granted length and continuation count updated.
    var M = _motion;
    if (M && M.own === move && typeof M.extend === "function" && M.report && M.report.mv) {
      var s = policySeconds(+spec.secs || 0, 8, Infinity), mv = M.report.mv;
      var grew = M.extend(s);
      attempt.mv = { kind: "walk", gait: "trained", dir: dir, policy: mv.policy };
      _moveReport(attempt, "merged", grew ? "continued" : "already_going");
      attempt.ended = 1;
      _moveLast = M.report;
      if (grew) {
        mv.ext = (mv.ext || 0) + 1;
        mv.secs = grew;
      }
      try { logEv("walk_extend", { s: s, n: mv.ext || 0, grew: grew ? 1 : 0 }); } catch (e) {}
      return true;
    }
    // A seek waits for a full walk rather than a stub (runGait's defer); let it.
    var left = dutyLeftMs();
    if (typeof seekGoal !== "undefined" && seekGoal && left < Math.min((+spec.secs || 8) * 1000, dutyCapMs())) return false;
    attempt.mv = {
      kind: "walk", gait: "trained", dir: dir, direction: back ? "screen-backward" : "screen-forward",
      policy: "set:" + Object.keys(ownSet).join("+"),
    };
    if (+spec.secs > 0) attempt.mv.ask = +spec.secs;
    attempt.mv.secs = policySeconds(+spec.secs || 0, 8, left);
    start(ownSet, move, +spec.secs || 0, attempt, "", false, true);
    return true;
  };
})();

/* The emergency stop (owner, 2026-11-15): "stop stop", the word twice in a row, halts whatever
   the legs are doing the moment its transcript lands (apply.py ESTOP_PATCHES calls this first in
   heardText, before any model turn), so a robot heading off a ledge stops however slow the box's
   model is. Twice, so a "stop" said in passing or misheard doesn't. A trained walk or skill set
   ends at its next tick (its gone() sees _motion change) with the legs released to neutral. */
(function () {
  "use strict";
  var STOP2_RE = /\bstop\b[\s,.!?;:\u2026\u2013\u2014-]*\bstop\b/i;
  // A shouted "Stop! ... Stop!" can land as two clips, each a lone "stop".
  var STOP1_RE = /^[\s,.!?;:\u2026\u2013\u2014-]*stop[\s,.!?;:\u2026\u2013\u2014-]*$/i, PAIR_MS = 2500, lone = -Infinity;
  window.gbEStop = function (t) {
    t = String(t || "");
    var now = Date.now(), twice = STOP2_RE.test(t);
    if (!twice && STOP1_RE.test(t)) { twice = now - lone <= PAIR_MS; lone = twice ? -Infinity : now; }
    if (!twice) return false;
    // A follow still loading (gbFollowMe) isn't moving yet, but this stop is for it too.
    window._gbFollowStopT = now;
    var dry = !!(window.gbFollow && window.gbFollow.active());
    if (window.gbFollow) window.gbFollow.stop("estop");
    var M = typeof _motion !== "undefined" ? _motion : null;
    if (!M && dry) { try { setErr("Stopped."); } catch (e) {} return true; }
    if (!M) return false;
    // As upstream's own halt words end a move (next.html _recHalt): its end row says halt.
    M.why = "halt";
    stopMotion();
    try { logEv("estop", { kind: String(M.kind || "") }); } catch (e) {}
    try { setErr("Stopped."); } catch (e) {}
    return true;
  };
})();
