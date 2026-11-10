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
      body: JSON.stringify({ text: text, voice: "", model: "kokoro", speed: u.rate || 1 }),
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
    return status === 413 || status === 400 || (status === 409 && json && json.error === "newer");
  }

  var state = sync("GET", PATH + "/state");
  if (state.status === 204) {
    // A G2 session: the phone's own creature comes back if a persona had the key.
    var m0 = meta();
    if (m0.persona) {
      var own = null;
      try { own = store.getItem(OWN); } catch (e) {}
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
        // The phone's own creature (a G2 session's, or none) is set aside, not lost.
        var mine = null;
        try { mine = store.getItem(KEY); } catch (e) {}
        if (mine) put(OWN, mine);
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

  // 4. Every write of the soul goes to the box, a moment after the last one.
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
    if (!soul || !at.persona) return;
    var mine = gen;
    inflight = true;
    fetch(PATH, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ persona: at.persona, base: at.version, soul: soul }),
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
