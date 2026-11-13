/* GrowBot on the box: a live strip, top-left of the creature, of what it is doing right now
   (owner, 2026-11-12: "so I can witness what's going on", and above all see when it takes a
   picture). Each activity is a chip with a running clock; when it ends the chip stays, dimmed
   with its final time, for LINGER_MS, while newer chips push it aside.

   It watches from the outside rather than patching upstream's 12k-line page at each site:
   the box's /api calls (fetch), the body (the relay socket's act/routine/pose/stop frames),
   and upstream's own globals for the ear (vadOn/recording/listenMode) and the voice
   (speakingUntil, which every voice route sets and the face's mouth reads; an <audio> hook
   cannot see it, since iPhone's voice plays through one endless MediaStream element). The
   photo is the moment a model call carries a picture, not the camera's own encode: upstream
   refills its frame ring every 2.5 s whether or not anything looks at it. Loaded in <head> after the face and before any page
   script, so every fetch and socket the page makes goes through the wrappers. Nothing here
   may throw into the page: every hook falls through to the real call. ?status=off hides it. */
(function () {
  "use strict";

  var LINGER_MS = 5000;
  // A next line of speech, or the next leg command, within this long is the same activity.
  var MERGE_MS = 1500;
  var MOVE_IDLE_MS = 900;
  var SNAP_SHOW_MS = 1200;
  // Upstream's blips set speakingUntil 700 ms ahead; a spoken line's floor is over 1.2 s.
  var SPEAK_MIN_MS = 1000;
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
    // Tool calls: the page names each one as the model asks for it (gbToolPill in apply.py).
    recall: { icon: "🔎", label: "memory search", colour: "#9ad0ff" },
    clock: { icon: "🕐", label: "time", colour: "#ffe08a" },
    calc: { icon: "🧮", label: "math", colour: "#a6f0c6" },
    timer: { icon: "⏲️", label: "timer", colour: "#ffb08a" },
    brain: { icon: "🧠", label: "notes search", colour: "#c9a6ff" },
    jot: { icon: "✍️", label: "note", colour: "#f6c3ff" },
  };
  var TOOL_MIN_MS = 1500;

  var off = false;
  try {
    off = new URLSearchParams(location.search).get("status") === "off";
  } catch (e) {}

  var now = function () {
    return typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
  };

  // Display order, kept by order() at each render: index 0 sits at the left edge.
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

  // A tool runs on the page in a moment; its chip stays long enough to be read.
  function tool(kind) {
    var e = begin(kind);
    if (e) e.shownUntil = Math.max(e.shownUntil, now() + TOOL_MIN_MS);
    return e;
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
    return typeof body === "string" && newPicture(body) ? "see" : "think";
  }

  // A spoken turn re-sends the picture its warm-up already sent, and a warm-up resent while
  // the person talks may carry the same frame: the flash marks only a picture the model has
  // not been shown. A frame is known by its length and a slice from its middle; a jpeg's
  // bytes differ there from any other capture.
  var seen = [];
  function newPicture(body) {
    var re = /"url":"data:image\/[a-z]+;base64,([^"]+)"/g;
    var m;
    var fresh = false;
    var any = false;
    while ((m = re.exec(body))) {
      any = true;
      var b = m[1];
      var mid = b.length >> 1;
      var id = b.length + ":" + b.slice(mid, mid + 64);
      if (seen.indexOf(id) < 0) {
        fresh = true;
        seen.push(id);
        if (seen.length > 24) seen.shift();
      }
    }
    // A picture sent some other way (a link, not inline) is new each time.
    return fresh || (!any && body.indexOf('"image_url"') >= 0);
  }

  // The page aborts a reply the person talked over; that is a turn cut short, not a failure.
  function aborted(err) {
    return !!err && err.name === "AbortError";
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
          function (err) {
            end(e, !aborted(err));
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
      if (kind === "see") snap();
      var e = begin(kind);
      p.then(
        function (r) {
          whenRead(r, e);
        },
        function (err) {
          end(e, !aborted(err));
        }
      );
      return p;
    };
  }

  // The body: relay-transport.js frames every leg command as {"t":"act"|"routine"|"pose"|"stop"}.
  // A hold re-sends its pose every 200 ms for up to 10 min: the same frame, or (a timed hold)
  // two frames in turn, 1 degree apart. Standing still is not moving, so a frame equal to
  // either of the last two (bar its request id) does not count.
  var moving = null;
  var movingT = 0;
  var lastFrames = ["", ""];
  var lastFrameT = 0;
  if (!off && window.WebSocket) {
    var realSend = WebSocket.prototype.send;
    WebSocket.prototype.send = function (data) {
      try {
        var frame =
          typeof data === "string" && /^\{"t":"(act|routine|pose|stop)"/.test(data)
            ? data.replace(/"rid":\d+,?/, "")
            : "";
        if (frame) {
          // After a pause the same gesture again is a new move; a hold never pauses.
          var t = now();
          var repeat =
            t - lastFrameT < MOVE_IDLE_MS &&
            (frame === lastFrames[0] || frame === lastFrames[1]);
          lastFrames = [frame, lastFrames[0]];
          lastFrameT = t;
        }
        if (frame && !repeat) {
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

  // The ear and the voice: upstream's own state, polled.
  var hearing = null;
  var speaking = null;
  var speakSince = 0;
  function pollPage() {
    var on = !!(window.vadOn || window.recording);
    if (on && !hearing) hearing = begin("hear");
    else if (!on && hearing) {
      end(hearing);
      hearing = null;
    }
    var until = typeof window.speakingUntil === "number" ? window.speakingUntil : 0;
    var t = now();
    if (Date.now() >= until) {
      speakSince = 0;
      if (speaking) end(speaking);
      speaking = null;
    } else if (!speakSince) {
      speakSince = t;
    } else if (!speaking && t - speakSince >= SPEAK_MIN_MS) {
      speaking = begin("speak");
      if (speaking && speaking.open === 1 && speaking.start > speakSince) speaking.start = speakSince;
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
    "#gbStatus .c.done{opacity:.55;border-style:dashed;padding:3px 7px 3px 4px;animation:none}" +
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

  function isOpen(e, t) {
    return e.open > 0 || t < e.shownUntil;
  }

  // What is under way sits at the left edge, newest first, then what has finished, most recent
  // first. Start order alone let a long run (the ear, a reopened chip) end up behind chips
  // that started after it and finished long ago.
  function order(t) {
    entries.sort(function (a, b) {
      var ao = isOpen(a, t);
      var bo = isOpen(b, t);
      if (ao !== bo) return ao ? -1 : 1;
      return ao ? b.start - a.start : b.end - a.end;
    });
  }

  // Upstream's wake gate puts the GrowBot logo top-left, under the strip; while it is up the
  // strip drops below its nav bar instead.
  function topBelowNav() {
    var navs = document.querySelectorAll(".gb-entry-nav");
    for (var i = 0; i < navs.length; i++) {
      var r = navs[i].getBoundingClientRect();
      if (r.height > 0 && r.bottom > 0) return Math.round(r.bottom + 6) + "px";
    }
    return "";
  }

  function render() {
    timer = 0;
    pollPage();
    if (!root) return;
    var t = now();
    var top = topBelowNav();
    if (root.style.top !== top) root.style.top = top;
    order(t);
    var anyOpen = false;
    var kept = [];
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      var open = isOpen(e, t);
      if (open) anyOpen = true;
      var age = open ? 0 : t - e.end;
      if (!open && age > LINGER_MS + 500) {
        if (e.el && e.el.parentNode) e.el.parentNode.removeChild(e.el);
        continue;
      }
      // A chip still running is never cut: its end would land on nothing.
      if (!open && kept.length >= MAX_CHIPS) {
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
    // Push to talk and a muted mic leave the ear on but hear nothing on their own.
    var hearing = window.listenMode && (window.GB_MIC || "open") === "open";
    var base = anyOpen ? "" : hearing ? "listen" : "idle";
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
    // Re-inserting an element restarts its animations, so a reorder moves the finished chips
    // (which have none) around the running ones, never a running one past them.
    var same = root.children.length === want.length;
    for (var j = 0; same && j < want.length; j++) same = root.children[j] === want[j];
    if (!same) {
      for (var k = root.children.length - 1; k >= 0; k--) {
        var c = root.children[k];
        if (c.classList.contains("done") || want.indexOf(c) < 0) root.removeChild(c);
      }
      for (j = 0; j < want.length; j++) {
        if (root.children[j] !== want[j]) root.insertBefore(want[j], root.children[j] || null);
      }
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

  window.gbStatus = { begin: begin, end: end, snap: snap, tool: tool };
})();
