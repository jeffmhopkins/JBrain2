/* GrowBot's face, drawn at full resolution over upstream's pixel sky.
   Upstream draws the whole face into a 96 px wide canvas and upscales it: the mood sky, the
   stars, the sparks, and two 12 px rectangle eyes. apply.py keeps the sky, stars and sparks,
   skips the rectangle eyes, and calls draw() once per frame with the state the eyes were drawn
   from, so this file decides shapes only and the brain still sets nothing but `emotion`.
   A persona wears the face the owner picks for it in JBrain (migration 0259): growbot-box.js
   reads it with the soul into __gbBoxFace, and its poll's gb-box-state event changes it where
   the creature stands. Without a persona (a G2 session) it is localStorage gb_face, else the
   lantern (owner, 2026-11-11). ?face=lantern|ostrich|robot|pixel overrides both, and pixel is
   upstream's own face, which is also what a throw in draw() falls back to. */
(function () {
  "use strict";

  var DEFAULT = "lantern";
  var LW = 360, LH = 740;

  function valid(v) { return v === "pixel" || v === "ostrich" || v === "robot" || v === "lantern"; }
  var asked = "";
  try { asked = new URLSearchParams(location.search).get("face") || ""; } catch (e) {}
  function pick() {
    if (valid(asked)) return asked;
    if (valid(window.__gbBoxFace)) return window.__gbBoxFace;
    var v = "";
    try { v = localStorage.gb_face || ""; } catch (e) {}
    return valid(v) ? v : DEFAULT;
  }
  var face = pick();
  var broken = false;
  window.addEventListener("gb-box-state", function (ev) {
    var f = ev && ev.detail && ev.detail.face;
    if (valid(asked) || !valid(f) || f === face) return;
    face = f;
    broken = false;
  });

  /* Its colour, as on the panels: steel by default, and the creature recolours itself when
     asked ("turn gold") through the reply's `coat` verb, the way `sky` washes the sky. Kept in
     localStorage gb_face_color so it is still that colour after a reload; ?color= sets it too. */
  var STEEL = "#7FA7C9";
  var NAMED = { white: "#eef3f8", silver: "#b8c4cf", grey: "#9aa4ae", gray: "#9aa4ae", black: "#46505c" };
  function hsl2hex(h, sat, l) {
    var a = sat * Math.min(l, 1 - l);
    function f(n) {
      var k = (n + h / 30) % 12, v = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
      return ("0" + Math.round(v * 255).toString(16)).slice(-2);
    }
    return "#" + f(0) + f(8) + f(4);
  }
  /* A word or hue to a body colour. `hue` is upstream's own _hueOf(v), so a colour the sky
     understands, the body understands too; null means it named no colour, and nothing changes. */
  function colourOf(v, hue) {
    var w = String(v == null ? "" : v).toLowerCase().trim();
    if (!w || /default|steel|normal/.test(w)) return STEEL;
    if (/^#[0-9a-f]{6}$/.test(w)) return w;
    for (var k in NAMED) if (w.indexOf(k) >= 0) return NAMED[k];
    return hue == null ? null : hsl2hex(hue, 0.7, 0.62);
  }
  var BODY = STEEL, custom = false, pending = "";
  /* Resolved at the first frame, not here: this file loads before upstream defines _hueOf, and
     ?color=gold needs it as much as the brain's coat does. */
  try { pending = new URLSearchParams(location.search).get("color") || ""; } catch (e) {}
  if (!pending) { try { pending = localStorage.gb_face_color || ""; } catch (e) {} }
  function setColour(v, hue) {
    var c = colourOf(v, hue);
    if (!c) return false;
    BODY = c;
    custom = c !== STEEL;
    try { if (custom) localStorage.gb_face_color = c; else localStorage.removeItem("gb_face_color"); } catch (e) {}
    return true;
  }
  /* GrowBot's seven emotions on the panels' Cozmo-style lid geometry (frontend/src/pet/face.ts
     SPECS): uy/ly are lid cover 0-1, ua the upper lid's tilt, lb the cheek-raise bend. Curious
     is symmetric here, unlike the panels': it is the creature's resting emotion (upstream starts
     on it and falls back to it), and lopsided lids at rest read as one scrunched eye (owner,
     2026-11-12). */
  function e(uy, ua, ly, lb, sx, sy) { return { uy: uy, ua: ua, ly: ly, lb: lb, sx: sx, sy: sy }; }
  var LIDS = {
    calm: { l: e(0.12, 0, 0.14, 0.18, 1, 1), tilt: 0, mouth: "small", crest: 0 },
    curious: { l: e(0, 0, 0.06, 0.1, 1.02, 1.02), tilt: 0, mouth: "small", crest: 0.5 },
    happy: { l: e(0, 0, 0.4, 0.4, 1, 1), tilt: 0, mouth: "smile", crest: 0.6 },
    alert: { l: e(0, 0, 0, 0, 1.1, 1.15), tilt: 0, mouth: "o", crest: 1 },
    scared: { l: e(0, 26, 0.35, 0.1, 1.2, 1.3), tilt: 0, mouth: "o", crest: 1 },
    sleepy: { l: e(0.48, 4, 0.45, 0, 1, 0.9), tilt: 3, mouth: "flat", crest: -0.4 },
    tired: { l: e(0.55, -8, 0.3, 0, 0.95, 0.85), tilt: 5, mouth: "flat", crest: -0.6 }
  };
  function spec(emotion) {
    var s = LIDS[emotion] || LIDS.curious, l = s.l;
    var r = s.r || { uy: l.uy, ua: -l.ua, ly: l.ly, lb: l.lb, sx: l.sx, sy: l.sy };
    return { l: l, r: r, tilt: s.tilt, mouth: s.mouth, crest: s.crest };
  }
  function copy(o) { var c = {}; for (var k in o) c[k] = o[k]; return c; }

  /* The pupil is the creature's life: a face that hid it for a whole mood (sleepy's lids met,
     happy's cheek swallowed it) read as no one home (owner, 2026-11-12). So at the eye's
     centreline the lids together may cover at most 1 - OPEN_MIN of it; past that they are
     scaled back as one, keeping the mood's shape. `bend` is how far each face's lower-lid curve
     peaks above its edge per unit of lb. Only a blink or sleep shuts an eye now. */
  var OPEN_MIN = 0.36, PANEL_BEND = 0.5, LANTERN_BEND = 1.2;
  function lidCap(p, bend) {
    var low = p.ly > 0.01 ? p.ly + bend * p.lb : 0, cover = p.uy + low;
    var k = cover > 1 - OPEN_MIN ? (1 - OPEN_MIN) / cover : 1;
    return { uy: p.uy * k, ua: p.ua, ly: p.ly * k, lb: p.lb * k, sx: p.sx, sy: p.sy, top: p.uy * k, bottom: low * k };
  }
  /* The pupil sits in the middle of what the lids leave open, sized to fit it, and the gaze
     moves it only as far as that opening allows; y is from the eye's centre, h its height. */
  function pupilIn(q, h, r0, gy) {
    var top = -h / 2 + h * q.top, room = h * (1 - q.top - q.bottom);
    var r = Math.min(r0, room * 0.42);
    return { y: top + room / 2 + gy * Math.max(0, room / 2 - r), r: r };
  }

  /* Easing lives here, not in upstream's P: P carries only eye and smile, and its numbers feed
     other code, so the shapes this file adds are eased beside it rather than written into it. */
  var cur = null, lastEmo = "", talk = 0, startle = 0, blush = 0, lastTouch = 0;
  var pup = { x: 0, y: 0 }, gaze = { x: 0, y: 0 }, lastT = 0;

  /* The phone's roll in the screen's plane, radians, + when its right side is down (owner,
     2026-11-12: the face should feel the phone tilt). Read from gravity in our own listener;
     upstream asks iOS for motion permission and learns the browser's accel sign into _IOS_G,
     which this multiplies by, so a flipped browser does not roll the face the wrong way. Lying
     flat, gravity leaves the screen's plane and the roll fades out rather than spin. */
  var roll = 0, rollAt = 0;
  try {
    window.addEventListener("devicemotion", function (ev) {
      var g = ev.accelerationIncludingGravity;
      if (!g || typeof g.x !== "number" || typeof g.y !== "number") return;
      var sg = window._IOS_G === -1 ? -1 : 1, x = sg * g.x, y = sg * g.y, m = Math.sqrt(x * x + y * y);
      if (!isFinite(m)) return;
      /* Gravity is in the device's frame and the face is drawn in the screen's: a phone turned
         to landscape has rotated its screen too, so the screen's angle comes back off. */
      var turn = 0;
      try {
        turn = screen.orientation && typeof screen.orientation.angle === "number" ? screen.orientation.angle : +window.orientation || 0;
      } catch (e) {}
      var a = m > 1 ? Math.atan2(-x, y) + turn * Math.PI / 180 : 0;
      a = Math.atan2(Math.sin(a), Math.cos(a));
      roll = a * Math.min(1, Math.max(0, (m - 3) / 4));
      rollAt = Date.now();
    });
  } catch (e) {}
  var lean = 0;

  function shade(hex, f) {
    var n = parseInt(hex.slice(1), 16);
    return "rgb(" + Math.round(((n >> 16) & 255) * f) + "," + Math.round(((n >> 8) & 255) * f) + "," + Math.round((n & 255) * f) + ")";
  }
  /* Own rounded rect: CanvasRenderingContext2D.roundRect is missing on the iOS 15 phones a
     GrowBot is often an old one of. */
  function rr(c, x, y, w, h, r) {
    r = Math.max(0, Math.min(r, w / 2, h / 2));
    c.beginPath();
    c.moveTo(x + r, y);
    c.arcTo(x + w, y, x + w, y + h, r);
    c.arcTo(x + w, y + h, x, y + h, r);
    c.arcTo(x, y + h, x, y, r);
    c.arcTo(x, y, x + w, y, r);
    c.closePath();
  }
  /* Necks are a stack of rings, the panels' own neck, standing on a body: a ringed neck
     with nothing under it read as too long, and a solid one lost the panels' look. */
  function rings(c, col, x0, y0, x1, y1, w0, w1, n, h) {
    c.fillStyle = col;
    for (var i = 0; i < n; i++) {
      var t = n > 1 ? i / (n - 1) : 0, w = w0 + (w1 - w0) * t;
      rr(c, x0 + (x1 - x0) * t - w / 2, y0 + (y1 - y0) * t - h / 2, w, h, h / 2);
      c.fill();
    }
  }
  /* The ostrich's body, cropped by the bottom of the screen: the round body, the tail plumes
     behind it, and the wing with its scalloped line-work (face.c draw_wing). */
  function birdBody(c, hex, d) {
    var s = 1.9, x = LW / 2, y = 590, sway = Math.sin(d.t / 1300) * 3, i;
    var TAIL = [[98, 70], [116, 80], [134, 66]];
    for (i = 0; i < 3; i++) {
      c.save();
      c.translate(x - 56 * s, y + 40 * s);
      c.rotate((TAIL[i][0] + sway) * Math.PI / 180);
      c.fillStyle = hex;
      rr(c, -8 * s, 0, 16 * s, TAIL[i][1] * s, 8 * s);
      c.fill();
      c.beginPath(); c.arc(0, TAIL[i][1] * s, 10 * s, 0, Math.PI * 2); c.fill();
      c.restore();
    }
    c.fillStyle = shade(hex, 0.86);
    rr(c, x - 72 * s, y, 144 * s, 112 * s, 54 * s);
    c.fill();
    var wx = x + 2 * s, wy = y + 12 * s, ww = 74 * s, wh = 66 * s;
    c.fillStyle = shade(hex, 0.64);
    rr(c, wx, wy, ww, wh, 30 * s);
    c.fill();
    c.strokeStyle = hex;
    c.lineWidth = 3 * s;
    c.lineCap = "round";
    for (var r = 20; r <= 48; r += 14) {
      c.beginPath();
      c.arc(wx + 2 * s, wy + 8 * s, r * s, Math.PI * 0.06, Math.PI * 0.44);
      c.stroke();
    }
  }

  function disc(c, x, y, r) { c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill(); }

  function zees(c, d, x, y) {
    if (!d.dreaming) return;
    c.fillStyle = "#cfc3ff";
    for (var i = 0; i < 3; i++) {
      var k = ((d.t / 1400 + i / 3) % 1 + 1) % 1;
      c.globalAlpha = Math.sin(k * Math.PI);
      c.font = (12 + i * 6) + "px 'Press Start 2P', ui-monospace, monospace";
      c.fillText("z", x + i * 22 + k * 10, y - i * 26 - k * 30);
    }
    c.globalAlpha = 1;
  }

  /* The panels' eye (firmware/main/face.c draw_eye): white rounded rect, pupil and catchlight,
     an upper lid tilted by ua, a lower lid bent into the cheek raise that makes happy read.
     A shut eye is a lid LINE: drawn as nothing, a blink reads as the face breaking. */
  function panelEye(c, cx, cy, s, p, d, dark) {
    var open = d.open;
    var w0 = 52 * p.sx * s * (1 + 0.2 * d.startle);
    var h = 62 * p.sy * s * open * (1 + 0.2 * d.startle);
    var bw = w0 * (1 + 0.35 * Math.max(0, 1 - Math.min(open, 1)));
    if (h < 6 * s) {
      c.fillStyle = dark;
      rr(c, cx - bw / 2, cy - 2.5 * s, bw, 5 * s, 2.5 * s);
      c.fill();
      return;
    }
    p = lidCap(p, PANEL_BEND);
    c.save();
    c.fillStyle = "#f6f9fc";
    rr(c, cx - bw / 2, cy - h / 2, bw, h, Math.min(bw, h) * 0.38);
    c.fill();
    c.clip();
    var pu = pupilIn(p, h, Math.min(h / 2, 17 * s * (1 - 0.3 * d.startle)), d.pupil.y);
    var pr = pu.r, px = cx + d.pupil.x * 11 * s, py = cy + pu.y;
    c.fillStyle = dark;
    disc(c, px, py, pr);
    c.fillStyle = "rgba(255,255,255,.92)";
    disc(c, px - pr * 0.35, py - pr * 0.41, pr * 0.3);
    c.fillStyle = "#000";
    if (p.uy > 0.01) {
      c.save();
      c.translate(cx, cy - h / 2);
      c.rotate(p.ua * Math.PI / 180);
      c.fillRect(-bw, -h, bw * 2, h + h * p.uy);
      c.restore();
    }
    if (p.ly > 0.01) {
      var ly = cy + h / 2 - h * p.ly;
      c.beginPath();
      c.moveTo(cx - bw, cy + h / 2 + 2);
      c.lineTo(cx - bw, ly);
      c.quadraticCurveTo(cx, ly - h * p.lb, cx + bw, ly);
      c.lineTo(cx + bw, cy + h / 2 + 2);
      c.closePath();
      c.fill();
    }
    c.restore();
  }

  /* A: the panels' ostrich (face.c draw_ostrich, docs/mocks/room-endpoint/ostrich-mock.py) at
     its own proportions, cropped to head and neck because a GrowBot has real legs. */
  function ostrich(c, d) {
    var hex = BODY, s = 2.2;
    var head = shade(hex, 0.94), beak = shade(hex, 0.52), dark = shade(hex, 0.22);
    var cx = LW / 2 + d.gaze.x * 8 + d.sway * 34, hy = 250 + d.bob + d.gaze.y * 6, i;
    birdBody(c, hex, d);
    rings(c, shade(hex, 0.76), LW / 2, 598, cx, hy + 42 * s, 50 * s * 0.95, 34 * s * 0.95, 7, 22);
    c.save();
    c.translate(cx, hy);
    c.rotate(d.tilt * Math.PI / 180);
    var CREST = [[-18, 42], [0, 50], [18, 42]];
    for (i = 0; i < 3; i++) {
      var len = CREST[i][1] * s * (0.85 + d.crest * 0.2);
      c.save();
      c.translate(0, -98 * s * 0.5 + 6);
      c.rotate((CREST[i][0] * (1 + d.crest * 0.6) + Math.sin(d.t / 420 + CREST[i][0]) * 2 * d.energy) * Math.PI / 180);
      c.fillStyle = hex;
      rr(c, -4.5 * s, -len, 9 * s, len, 4.5 * s);
      c.fill();
      disc(c, 0, -len, 9 * s * 0.62);
      c.restore();
    }
    var HW = 128 * s, HH = 98 * s;
    c.fillStyle = head;
    rr(c, -HW / 2, -HH / 2, HW, HH, 44 * s);
    c.fill();
    if (d.blush > 0.01) {
      c.fillStyle = "rgba(255,120,170," + (0.45 * d.blush) + ")";
      for (i = -1; i <= 1; i += 2) { c.beginPath(); c.ellipse(i * 50 * s, 22 * s, 13 * s, 8 * s, 0, 0, Math.PI * 2); c.fill(); }
    }
    panelEye(c, -31 * s, -7 * s, s * 0.86, d.lids.l, d, dark);
    panelEye(c, 31 * s, -7 * s, s * 0.86, d.lids.r, d, dark);
    /* The lower mandible drops and the upper does not, which is how a beak opens; the gape is
       most of the beak's height because a talking mouth has to read across a room (face.c). */
    var gape = d.talk * 30;
    if (gape > 1) {
      c.fillStyle = shade(hex, 0.12);
      rr(c, -17 * s, 38 * s, 34 * s, (gape * 0.9 + 6) * s, 6 * s);
      c.fill();
    }
    var BEAK = [[42, 13, -112], [33, 12, -101], [23, 11, -91], [13, 10, -82]];
    c.fillStyle = beak;
    for (i = 0; i < 4; i++) {
      rr(c, -BEAK[i][0] / 2 * s, (BEAK[i][2] + 137 + (i >= 1 ? gape * i / 3 : 0)) * s, BEAK[i][0] * s, BEAK[i][1] * s, 5 * s);
      c.fill();
    }
    c.fillStyle = shade(hex, 0.28);
    c.fillRect(-16 * s, 40 * s, 32 * s, 2 * s);
    c.restore();
    zees(c, d, cx + 100, hy - 60);
  }

  /* B: the panels' robot head (face.c draw_robot) with a crop of the chest. */
  function robot(c, d) {
    var hex = BODY, s = 1.45, dark = shade(hex, 0.22), i;
    var cx = LW / 2 + d.gaze.x * 6 + d.sway * 30, hy = 300 + d.bob + d.gaze.y * 5;
    c.fillStyle = shade(hex, 0.88);
    rr(c, LW / 2 - 144 * s * 0.9, 560, 288 * s * 0.9, 300, 70);
    c.fill();
    c.fillStyle = shade(hex, 0.55);
    rr(c, LW / 2 - 40 * s, 610, 80 * s, 60 * s, 16 * s);
    c.fill();
    c.fillStyle = d.talking ? d.glow : shade(hex, 0.4);
    disc(c, LW / 2, 610 + 30 * s, 9 * s);
    c.fillStyle = shade(hex, 0.7);
    rr(c, LW / 2 - 34 * s, 530, 68 * s, 40, 12);
    c.fill();
    rings(c, shade(hex, 0.7), LW / 2, 532, cx, hy + 88 * s + 10, 44 * s, 38 * s, 4, 18);
    c.save();
    c.translate(cx, hy);
    c.rotate(d.tilt * Math.PI / 180);
    var HW = 108 * s, HH = 88 * s;
    c.fillStyle = hex;
    c.fillRect(-4 * s, -HH - 30 * s, 8 * s, 30 * s);
    c.fillStyle = d.think ? d.glow : hex;
    disc(c, 0, -HH - 36 * s, (11 + d.crest * 3) * s);
    c.fillStyle = hex;
    rr(c, -HW, -HH, HW * 2, HH * 2, 40 * s);
    c.fill();
    if (d.blush > 0.01) {
      c.fillStyle = "rgba(255,120,170," + (0.45 * d.blush) + ")";
      for (i = -1; i <= 1; i += 2) { c.beginPath(); c.ellipse(i * 78 * s, 26 * s, 16 * s, 10 * s, 0, 0, Math.PI * 2); c.fill(); }
    }
    panelEye(c, -HW * 0.43, -HH * 0.17, s, d.lids.l, d, dark);
    panelEye(c, HW * 0.43, -HH * 0.17, s, d.lids.r, d, dark);
    var my = HH * 0.52 - 16 * s, mr = 30 * s;
    c.fillStyle = dark;
    c.strokeStyle = dark;
    c.lineCap = "round";
    c.lineWidth = 9 * s;
    if (d.mouth === "o") {
      c.beginPath();
      c.ellipse(0, my + 18 * s, 18 * s, (20 + d.talk * 10) * s, 0, 0, Math.PI * 2);
      c.fill();
    } else {
      /* The mouth opens OUT of the smile line, so the smile becomes its bottom lip and the gape
         collapses back onto the arc with no second shape (face.c). */
      var bend = d.mouth === "flat" ? 0.08 : d.mouth === "small" ? 0.6 : 1;
      var gape = d.talk * 26 * s, xmax = Math.cos(Math.PI * 0.15) * mr, dx;
      var lip = function (x) { return my + Math.sqrt(mr * mr - x * x) * bend + (1 - bend) * mr * 0.8; };
      if (gape > 0.5) {
        c.beginPath();
        for (dx = -xmax; dx <= xmax; dx += 2) c.lineTo(dx, lip(dx));
        for (dx = xmax; dx >= -xmax; dx -= 2) c.lineTo(dx, lip(dx) - gape * Math.sqrt(Math.max(0, 1 - (dx / xmax) * (dx / xmax))));
        c.closePath();
        c.fill();
      }
      c.beginPath();
      for (dx = -xmax; dx <= xmax; dx += 2) c.lineTo(dx, lip(dx));
      c.stroke();
    }
    c.restore();
    zees(c, d, cx + 110, hy - 90);
  }

  /* C: lantern eyes. The eyes go on their own layer with the lids CUT OUT, and the layer is
     glowed as one image: a halo per shape would glow around the lidded part too. */
  var layerC = null;
  function lantern(c, d) {
    var glow = d.glow, cx = LW / 2 + d.gaze.x * 22 + d.sway * 44, cy = 300 + d.bob + d.gaze.y * 16, gap = 74;
    if (!layerC) layerC = document.createElement("canvas");
    if (layerC.width !== c.canvas.width || layerC.height !== c.canvas.height) { layerC.width = c.canvas.width; layerC.height = c.canvas.height; }
    var o = layerC.getContext("2d"), m = c.getTransform();
    o.setTransform(1, 0, 0, 1, 0, 0);
    o.clearRect(0, 0, layerC.width, layerC.height);
    o.setTransform(m);
    o.translate(cx, cy);
    o.rotate(d.tilt * Math.PI / 180 * 0.6);
    for (var side = -1; side <= 1; side += 2) {
      var p = lidCap(side < 0 ? d.lids.l : d.lids.r, LANTERN_BEND);
      var w = 104 * p.sx * (1 + 0.15 * d.startle), h = 150 * p.sy * (1 + 0.15 * d.startle) * d.open;
      var bw = w * (1 + 0.3 * Math.max(0, 1 - Math.min(d.open, 1))), x = side * gap;
      o.globalCompositeOperation = "source-over";
      o.fillStyle = glow;
      if (h < 12) { rr(o, x - bw / 2, -5, bw, 10, 5); o.fill(); continue; }
      rr(o, x - bw / 2, -h / 2, bw, h, Math.min(bw, h) * 0.42);
      o.fill();
      o.globalCompositeOperation = "source-atop";
      o.fillStyle = "rgba(255,255,255,.35)";
      rr(o, x - bw * 0.28, -h / 2 + h * 0.12, bw * 0.2, h * 0.22, bw * 0.1);
      o.fill();
      var pu = pupilIn(p, h, Math.min(bw, h) * 0.17, d.pupil.y);
      o.fillStyle = "rgba(0,0,0,.78)";
      disc(o, x + d.pupil.x * bw * 0.22, pu.y, pu.r);
      o.globalCompositeOperation = "destination-out";
      o.fillStyle = "#000";
      if (p.uy > 0.01) {
        o.save(); o.translate(x, -h / 2); o.rotate(p.ua * Math.PI / 180);
        o.fillRect(-bw, -h, bw * 2, h + h * p.uy); o.restore();
      }
      if (p.ly > 0.01) {
        var edge = h / 2 - h * p.ly * (1 - p.lb * 2), peak = h / 2 - h * p.ly - h * p.lb * 1.2;
        o.beginPath();
        o.moveTo(x - bw, h / 2 + 4);
        o.lineTo(x - bw, edge);
        o.quadraticCurveTo(x, 2 * peak - edge, x + bw, edge);
        o.lineTo(x + bw, h / 2 + 4);
        o.closePath();
        o.fill();
      }
    }
    o.globalCompositeOperation = "source-over";
    c.save();
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.shadowColor = glow;
    c.shadowBlur = (30 + 18 * d.energy) * m.a;
    c.drawImage(layerC, 0, 0);
    c.restore();
    c.save();
    c.translate(cx, cy);
    c.rotate(d.tilt * Math.PI / 180 * 0.6);
    c.shadowColor = glow;
    c.shadowBlur = 18 * m.a;
    c.strokeStyle = glow;
    c.globalAlpha = d.talking ? 0.95 : 0.25 + 0.15 * Math.max(0, d.smile);
    c.lineWidth = 6;
    c.lineCap = "round";
    c.beginPath();
    var curve = d.talking ? 0 : Math.max(-0.6, Math.min(1, d.smile)) * 16;
    for (var i = 0; i <= 40; i++) {
      var u = i / 40, env = Math.sin(u * Math.PI);
      var wave = d.talking ? Math.sin(u * 14 + d.t / 70) * d.talk * 22 * env : 0;
      c.lineTo(-70 + u * 140, 150 + wave + curve * env);
    }
    c.stroke();
    c.restore();
    zees(c, d, cx + 110, cy - 90);
  }

  var FACES = { ostrich: ostrich, robot: robot, lantern: lantern };

  /* One frame. `s` is what upstream's drawFace computed for its own eyes: t, emotion, eye
     (P.eye), smile, energy, blink, paused, dreaming, think, orbit, looking, gx/gy (the glance
     at a touch, -1..1), shake, talking, glow. W/H are CSS px; cx already carries the DPR. */
  function draw(c, W, H, s) {
    var sp = spec(s.emotion), k, side;
    if (!cur) cur = { l: copy(sp.l), r: copy(sp.r), tilt: sp.tilt, crest: sp.crest };
    if (s.emotion !== lastEmo) {
      if (lastEmo && (s.emotion === "scared" || s.emotion === "alert")) startle = 1;
      lastEmo = s.emotion;
    }
    for (side = 0; side < 2; side++) {
      var a = side ? cur.r : cur.l, b = side ? sp.r : sp.l;
      for (k in b) a[k] += (b[k] - a[k]) * 0.15;
    }
    cur.tilt += (sp.tilt - cur.tilt) * 0.08;
    cur.crest += (sp.crest - cur.crest) * 0.08;
    startle *= 0.94;
    if (s.touchT && s.touchT !== lastTouch) { lastTouch = s.touchT; blush = 1; }
    blush *= 0.97;
    var talking = s.talking && !s.dreaming;
    var syll = talking ? Math.max(0, Math.sin(s.t / 85) * 0.6 + Math.sin(s.t / 37) * 0.4) : 0;
    talk += (syll - talk) * 0.45;

    /* Upstream's openness runs 0.06-1.3 with the emotion folded in; here the lid shape carries
       the emotion, so openness only nudges it, and the states that override it do so here. */
    var open = 0.75 + 0.25 * Math.min(1.3, s.eye) + Math.max(-0.15, Math.min(0.15, (s.energy - 0.4) * 0.3));
    if (s.paused) open = 0.35 + 0.08 * Math.sin(s.t / 900);
    if (s.dreaming || s.blink) open = 0.04;
    if (s.think && !s.blink) open = Math.max(open, 0.6);
    if (s.looking && !s.blink) open = 0.42 + 0.04 * Math.sin(s.t / 500);

    /* Upstream's think pupil jumps round an 8-step orbit every 150 ms and its touch glance moves
       in whole pixels; at this size both read as darting (owner, 2026-11-12). So thinking and
       looking become a slow left-right wander the whole eye follows a little, and every move is
       eased by time, not per frame, because LOW_POWER draws at 15 fps. */
    var dt = lastT && s.t > lastT ? Math.min(250, s.t - lastT) : 16;
    lastT = s.t;
    /* Tilted, the pupils roll to the low side, the head leans back toward level and the whole
       face swings a little downhill on its neck: gravity on the eyes, balance on the head
       (owner, 2026-11-12: the face itself reacts, not just the eyes). Only past a hand-held
       phone's wobble, capped so a phone on its side does not tip the face over, and slow, so
       a walking body does not shake it; a stale reading (no sensor, or permission never
       given) settles back to zero. */
    var tilt = Date.now() - rollAt < 1000 && Math.abs(roll) < 2.4 ? roll : 0;
    var felt = Math.sign(tilt) * Math.max(0, Math.min(0.7, Math.abs(tilt) - 0.08));
    lean += (felt - lean) * (1 - Math.exp(-dt / 400));
    var wander = s.think || s.looking ? Math.sin(s.t / 1600) * 0.7 : 0;
    var aim = s.think || s.looking ? { x: wander, y: 0 } : { x: s.gx, y: s.gy };
    aim.x = Math.max(-1.2, Math.min(1.2, aim.x + lean * 1.6));
    var ease = 1 - Math.exp(-dt / 320);
    pup.x += (aim.x - pup.x) * ease;
    pup.y += (aim.y - pup.y) * ease;
    gaze.x += (s.gx + wander * 0.4 - gaze.x) * ease;
    gaze.y += (s.gy - gaze.y) * ease;
    var bob = (s.paused || s.dreaming ? 6 * Math.sin(s.t / 900) : 3 * Math.sin(s.t / 1300)) + Math.sin(s.t * 0.05) * s.shake * 8;
    var d = {
      t: s.t, open: open, lids: cur, pupil: pup,
      gaze: { x: gaze.x + Math.sin(s.t * 0.045) * s.shake * 0.6, y: gaze.y }, think: s.think,
      dreaming: s.dreaming, talking: talking, talk: talk, smile: s.smile, mouth: sp.mouth,
      tilt: cur.tilt - lean * 180 / Math.PI * 0.45, sway: lean, crest: cur.crest, startle: startle, blush: blush,
      /* The lantern has no body, so a chosen colour is its eyes' colour; unchosen, the mood's. */
      glow: face === "lantern" && custom ? BODY : s.glow,
      energy: s.energy, bob: bob
    };
    /* The face proper is the 360x480 band from y=40 of the design, fitted into the top 62% of
       the canvas: the caption, the text box and the button bar own the rest, and the neck or
       chest below the band runs on behind them. */
    var z = Math.min(W / LW, H * 0.62 / 480);
    c.save();
    c.translate((W - LW * z) / 2, H * 0.02 - 40 * z);
    c.scale(z, z);
    c.imageSmoothingEnabled = true;
    FACES[face](c, d);
    c.restore();
  }

  window.gbFace = {
    /* getTransform is the one API here older WebViews lack; without it the throw would come
       before the catch below could mark the face broken, and the creature would have no eyes. */
    on: function () {
      return face !== "pixel" && !broken && typeof CanvasRenderingContext2D !== "undefined" &&
        typeof CanvasRenderingContext2D.prototype.getTransform === "function";
    },
    setColour: setColour,
    colour: function () { return BODY; },
    /* What each face's lids leave open at the eye's centreline, per emotion, 0-1: the pupil's
       guarantee, read by the site tests. */
    openings: function () {
      var out = {};
      for (var k in LIDS) {
        var sp = spec(k), o = { panel: 1, lantern: 1 };
        [sp.l, sp.r].forEach(function (p) {
          var a = lidCap(p, PANEL_BEND), b = lidCap(p, LANTERN_BEND);
          o.panel = Math.min(o.panel, 1 - a.top - a.bottom);
          o.lantern = Math.min(o.lantern, 1 - b.top - b.bottom);
        });
        out[k] = o;
      }
      return out;
    },
    draw: function (c, W, H, s) {
      var m = c.getTransform();
      try {
        if (pending) {
          var v = pending;
          pending = "";
          setColour(v, typeof window._hueOf === "function" ? window._hueOf(v) : null);
        }
        draw(c, W, H, s);
      } catch (err) {
        broken = true;
        /* A throw can leave states saved, a clip among them, which setTransform does not undo.
           Upstream never saves across a frame, so popping to an empty stack is exact; restore()
           on an empty stack does nothing, and no frame here nests near 64. */
        for (var i = 0; i < 64; i++) c.restore();
        c.setTransform(m);
        c.globalAlpha = 1;
        c.globalCompositeOperation = "source-over";
        c.shadowBlur = 0;
        try { console.warn("growbot-face: falling back to the pixel face", err); } catch (e) {}
      }
    }
  };
})();
