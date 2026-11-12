/* GrowBot on the box: a live strip, top-left of the creature, of what it is doing right now
   (owner, 2026-11-12: "so I can witness what's going on", and above all see when it takes a
   picture). Each activity is a chip with a running clock; when it ends the chip stays, dimmed
   with its final time, for LINGER_MS, while newer chips push it aside.

   It watches from the outside rather than patching upstream's 12k-line page at each site:
   the box's /api calls (fetch), the camera frame (canvas toDataURL), the voice (media play),
   the body (the relay socket's act/routine/pose/stop frames) and the ear (upstream's own
   vadOn/recording/listenMode globals). Loaded in <head> after the face and before any page
   script, so every fetch and socket the page makes goes through the wrappers. Nothing here
   may throw into the page: every hook falls through to the real call. ?status=off hides it. */
(function () {
  "use strict";

  var LINGER_MS = 5000;
  // A next line of speech, or the next leg command, within this long is the same activity.
  var MERGE_MS = 1500;
  var MOVE_IDLE_MS = 900;
  var SNAP_SHOW_MS = 1200;
  var MAX_CHIPS = 5;

  var KINDS = {
    snap: { icon: "📷", label: "photo", colour: "#ffffff" },
    see: { icon: "👁️", label: "looking", colour: "#ffb347" },
    think: { icon: "💭", label: "thinking", colour: "#b48cff" },
    hear: { icon: "👂", label: "hearing", colour: "#74ffb4" },
    stt: { icon: "📝", label: "transcribing", colour: "#5fd0ff" },
    voice: { icon: "🎙️", label: "voicing", colour: "#ff8ad8" },
    speak: { icon: "🗣️", label: "speaking", colour: "#ff6fae" },
    move: { icon: "🦿", label: "moving", colour: "#ffd84a" },
    listen: { icon: "🎧", label: "listening", colour: "#74ffb4" },
    idle: { icon: "💤", label: "idle", colour: "#7f8ba3" },
  };

  var off = false;
  try {
    off = new URLSearchParams(location.search).get("status") === "off";
  } catch (e) {}

  var now = function () {
    return typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
  };

  // Newest first: index 0 sits at the left edge, so a new chip pushes the rest right.
  var entries = [];
  var baseKind = "";
  var baseSince = now();

  function newest(kind) {
    for (var i = 0; i < entries.length; i++) if (entries[i].kind === kind) return entries[i];
    return null;
  }

  function begin(kind) {
    if (!KINDS[kind]) return null;
    var t = now();
    var e = newest(kind);
    if (e && (e.open > 0 || t - e.end < MERGE_MS) && !e.failed) {
      if (e.open === 0) e.end = 0;
      e.open++;
    } else {
      e = { kind: kind, start: t, end: 0, open: 1, failed: false, el: null, shownUntil: 0 };
      entries.unshift(e);
    }
    schedule();
    return e;
  }

  function end(e, failed) {
    if (!e || e.open === 0) return;
    e.open--;
    if (failed) e.failed = true;
    if (e.open === 0) e.end = Math.max(now(), e.shownUntil);
    schedule();
  }

  function snap() {
    var e = begin("snap");
    if (!e) return;
    e.shownUntil = now() + SNAP_SHOW_MS;
    end(e);
    flash();
  }

  // -- the hooks -------------------------------------------------------------------------

  function kindOfCall(input, init) {
    var url = typeof input === "string" ? input : input && input.url;
    if (!url) return "";
    var u = new URL(url, location.href);
    if (u.origin !== location.origin) return "";
    if (u.pathname === "/api/fish-tts") return "voice";
    if (u.pathname !== "/api/llm" && u.pathname !== "/api/llm-stream") return "";
    var target = u.searchParams.get("target") || "";
    if (/-audio$/.test(target)) return "stt";
    if (target !== "openrouter") return "";
    var body = init && init.body;
    return typeof body === "string" && body.indexOf('"image_url"') >= 0 ? "see" : "think";
  }

  // The call lasts until its body has been read, not until its headers: a streamed reply is
  // still thinking while its tokens arrive.
  function whenRead(response, e) {
    try {
      var copy = response.clone();
      if (!copy.body || !copy.body.getReader) {
        end(e, !response.ok);
        return;
      }
      var reader = copy.body.getReader();
      var pump = function () {
        reader.read().then(
          function (r) {
            if (r.done) end(e, !response.ok);
            else pump();
          },
          function () {
            end(e, true);
          }
        );
      };
      pump();
    } catch (err) {
      end(e, !response.ok);
    }
  }

  if (!off && typeof window.fetch === "function") {
    var realFetch = window.fetch;
    window.fetch = function (input, init) {
      var p = realFetch.apply(this, arguments);
      var kind = "";
      try {
        kind = kindOfCall(input, init);
      } catch (err) {}
      if (!kind) return p;
      var e = begin(kind);
      p.then(
        function (r) {
          whenRead(r, e);
        },
        function () {
          end(e, true);
        }
      );
      return p;
    };
  }

  // Upstream takes its picture with captureEye's canvas toDataURL("image/jpeg"); the 48x36
  // luma sensor never encodes, so any JPEG worth a model's look is a photo.
  if (!off && window.HTMLCanvasElement) {
    var realToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function (type) {
      var out = realToDataURL.apply(this, arguments);
      try {
        if (String(type || "").indexOf("jpeg") >= 0 && this.width >= 96) snap();
      } catch (err) {}
      return out;
    };
  }

  // The voice: any clip longer than the silent unlock blip growbot-box.js plays on a tap.
  var playing = typeof WeakMap === "function" ? new WeakMap() : null;
  function watchPlay(el) {
    if (!playing || !el || el.__gbStatus) return;
    el.__gbStatus = true;
    el.addEventListener("playing", function () {
      if (playing.get(el)) return;
      var d = el.duration;
      if (isFinite(d) && d < 0.35) return;
      playing.set(el, begin("speak"));
    });
    var stop = function () {
      var e = playing.get(el);
      if (!e) return;
      playing.delete(el);
      end(e);
    };
    ["ended", "pause", "error", "emptied", "abort"].forEach(function (t) {
      el.addEventListener(t, stop);
    });
  }
  if (!off && window.HTMLMediaElement) {
    var realPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      try {
        if (this instanceof HTMLAudioElement) watchPlay(this);
      } catch (err) {}
      return realPlay.apply(this, arguments);
    };
  }

  // The body: relay-transport.js frames every leg command as {"t":"act"|"routine"|"pose"|"stop"}.
  var moving = null;
  var movingT = 0;
  if (!off && window.WebSocket) {
    var realSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
      try {
        if (typeof data === "string" && /^\{"t":"(act|routine|pose|stop)"/.test(data)) {
          if (!moving) moving = begin("move");
          clearTimeout(movingT);
          movingT = setTimeout(function () {
            end(moving);
            moving = null;
          }, MOVE_IDLE_MS);
        }
      } catch (err) {}
      return realSend.apply(this, arguments);
    };
  }

  // The ear: upstream's own state, polled, since its VAD lives in closures over globals.
  var hearing = null;
  function pollEar() {
    var on = !!(window.vadOn || window.recording);
    if (on && !hearing) hearing = begin("hear");
    else if (!on && hearing) {
      end(hearing);
      hearing = null;
    }
  }

  // -- the strip -------------------------------------------------------------------------

  var root = null;
  var flashEl = null;

  var CSS =
    "#gbStatus{position:fixed;z-index:2147483000;pointer-events:none;" +
    "top:calc(env(safe-area-inset-top,0px) + 10px);left:calc(env(safe-area-inset-left,0px) + 10px);" +
    "right:10px;display:flex;flex-direction:row;align-items:flex-start;gap:6px;overflow:hidden;" +
    "font:600 11px/1.15 system-ui,-apple-system,sans-serif;color:#e8eefc}" +
    "#gbStatus .c{flex:0 0 auto;display:flex;align-items:center;gap:6px;padding:5px 9px 5px 6px;" +
    "border-radius:14px;background:rgba(8,12,24,.78);border:2px solid var(--k);" +
    "transition:opacity .45s ease,transform .3s ease,max-width .3s ease;max-width:200px;" +
    "animation:gbIn .25s ease-out}" +
    "#gbStatus .c.on{box-shadow:0 0 12px var(--k);animation:gbIn .25s ease-out,gbPulse 1.2s ease-in-out infinite}" +
    "#gbStatus .c.done{opacity:.55;border-style:dashed;padding:3px 7px 3px 4px}" +
    // A finished chip keeps only its icon and its time, so three of them fit beside a live one.
    "#gbStatus .c.done .l{display:none}" +
    "#gbStatus .c.done .i{font-size:17px}" +
    "#gbStatus .c.gone{opacity:0}" +
    "#gbStatus .c.bad{border-color:#ff5a5a}" +
    "#gbStatus .i{font-size:22px;line-height:1}" +
    "#gbStatus .l{display:block;text-transform:uppercase;letter-spacing:.06em;font-size:10px;color:var(--k)}" +
    "#gbStatus .t{display:block;font:600 14px/1.1 ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}" +
    "#gbStatus .c.base{opacity:.7;border-width:1px;box-shadow:none;animation:none}" +
    "#gbFlash{position:fixed;inset:0;z-index:2147482999;pointer-events:none;opacity:0;" +
    "box-shadow:inset 0 0 0 8px #fff,inset 0 0 80px rgba(255,255,255,.55);background:rgba(255,255,255,.18)}" +
    "#gbFlash.go{animation:gbFlash .6s ease-out}" +
    "@keyframes gbIn{from{transform:translateX(-24px);opacity:0}to{transform:none;opacity:1}}" +
    "@keyframes gbPulse{50%{box-shadow:0 0 2px var(--k)}}" +
    "@keyframes gbFlash{0%{opacity:1}100%{opacity:0}}";

  function mount() {
    if (root || off || !document.body) return;
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    flashEl = document.createElement("div");
    flashEl.id = "gbFlash";
    document.body.appendChild(flashEl);
    root = document.createElement("div");
    root.id = "gbStatus";
    root.setAttribute("aria-live", "polite");
    document.body.appendChild(root);
    schedule();
  }

  function flash() {
    if (!flashEl) return;
    flashEl.classList.remove("go");
    void flashEl.offsetWidth;
    flashEl.classList.add("go");
  }

  function clock(ms) {
    var s = Math.max(0, ms) / 1000;
    if (s < 10) return s.toFixed(1) + "s";
    if (s < 60) return Math.floor(s) + "s";
    var m = Math.floor(s / 60);
    var r = Math.floor(s % 60);
    return m + "m" + (r < 10 ? "0" : "") + r;
  }

  function chip(kind) {
    var k = KINDS[kind];
    var el = document.createElement("div");
    el.className = "c";
    el.style.setProperty("--k", k.colour);
    el.innerHTML = '<span class="i"></span><span><span class="l"></span><span class="t"></span></span>';
    el.firstChild.textContent = k.icon;
    el.querySelector(".l").textContent = k.label;
    return el;
  }

  var baseEl = null;
  var timer = 0;

  function render() {
    timer = 0;
    pollEar();
    if (!root) return;
    var t = now();
    var anyOpen = false;
    var kept = [];
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      var open = e.open > 0 || t < e.shownUntil;
      if (open) anyOpen = true;
      var age = open ? 0 : t - e.end;
      if (!open && age > LINGER_MS + 500) {
        if (e.el && e.el.parentNode) e.el.parentNode.removeChild(e.el);
        continue;
      }
      if (kept.length >= MAX_CHIPS) {
        if (e.el && e.el.parentNode) e.el.parentNode.removeChild(e.el);
        continue;
      }
      kept.push(e);
      if (!e.el) e.el = chip(e.kind);
      e.el.classList.toggle("on", open);
      e.el.classList.toggle("done", !open);
      e.el.classList.toggle("gone", !open && age > LINGER_MS);
      e.el.classList.toggle("bad", e.failed);
      e.el.querySelector(".t").textContent = clock((open ? t : e.end) - e.start);
    }
    entries = kept;

    // With nothing under way the strip still says what it is waiting on, and for how long.
    var base = anyOpen ? "" : window.listenMode ? "listen" : "idle";
    if (base !== baseKind) {
      baseKind = base;
      baseSince = t;
      if (baseEl && baseEl.parentNode) baseEl.parentNode.removeChild(baseEl);
      baseEl = base ? chip(base) : null;
      if (baseEl) baseEl.classList.add("base");
    }
    if (baseEl) baseEl.querySelector(".t").textContent = clock(t - baseSince);

    var want = (baseEl ? [baseEl] : []).concat(
      kept.map(function (e) {
        return e.el;
      })
    );
    for (var j = 0; j < want.length; j++) {
      if (root.children[j] !== want[j]) root.insertBefore(want[j], root.children[j] || null);
    }
    while (root.children.length > want.length) root.removeChild(root.lastChild);
    schedule();
  }

  function schedule() {
    if (!timer && !off) timer = setTimeout(render, 100);
  }

  if (!off) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
    else mount();
  }

  window.gbStatus = { begin: begin, end: end, snap: snap };
})();
