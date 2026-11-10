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
  var audio = null;

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

  function stopAudio() {
    if (!audio) return;
    try {
      audio.pause();
      if (audio.src) URL.revokeObjectURL(audio.src);
    } catch (e) {}
    audio = null;
  }

  function finish(type) {
    var u = current;
    current = null;
    stopAudio();
    synth.speaking = false;
    if (u) emit(u, type);
    pump();
  }

  function pump() {
    if (current || !queue.length) return;
    current = queue.shift();
    synth.speaking = true;
    var u = current;
    fetch("/api/fish-tts", {
      method: "POST",
      headers: { "content-type": "application/json", "x-fish-key": "box" },
      body: JSON.stringify({ text: String(u.text || ""), voice: "", model: "kokoro", speed: u.rate || 1 }),
    })
      .then(function (r) {
        if (r.status !== 200) throw r.status;
        return r.blob();
      })
      .then(function (blob) {
        if (current !== u) return;
        audio = new Audio(URL.createObjectURL(blob));
        audio.onended = function () { if (current === u) finish("end"); };
        audio.onerror = function () { if (current === u) finish("error"); };
        emit(u, "start");
        return audio.play();
      })
      .catch(function () {
        if (current === u) finish("error");
      });
  }

  var synth = {
    speaking: false,
    pending: false,
    paused: false,
    onvoiceschanged: null,
    getVoices: function () { return []; },
    speak: function (utterance) {
      queue.push(utterance);
      synth.pending = queue.length > 0;
      pump();
    },
    cancel: function () {
      queue = [];
      synth.pending = false;
      if (current) finish("end");
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
