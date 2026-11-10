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
