// The three candidate GrowBot faces. Each draws ONE instant of the shared state (`shared.js`
// owns the clock, the emotion easing, blinks, gaze and speech); a face decides shapes only, the
// same split the panel firmware keeps between `face.c` and its rig.
//
// Logical space is 360 x 740: a portrait phone, which is what GrowBot's screen is.
(function (root) {
  "use strict";

  const LW = 360;
  const LH = 740;

  function shade(hex, f) {
    const n = Number.parseInt(hex.slice(1), 16);
    const r = Math.round(((n >> 16) & 255) * f);
    const g = Math.round(((n >> 8) & 255) * f);
    const b = Math.round((n & 255) * f);
    return `rgb(${r},${g},${b})`;
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

  function rr(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, Math.max(0, Math.min(r, w / 2, h / 2)));
  }

  // The mood sky and its drifting stars, kept from today's face in every variant: the sky's hue
  // is where the creature's inner weather shows, and nothing else on screen carries it.
  function sky(ctx, d, strength) {
    const g = ctx.createLinearGradient(0, 0, 0, LH);
    g.addColorStop(0, `hsl(${d.skyHue} 45% ${4 + 5 * strength}%)`);
    g.addColorStop(1, "#000");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, LW, LH);
    ctx.fillStyle = `hsl(${d.starHue} 70% ${30 + d.energy * 30}% / ${0.35 + 0.5 * strength})`;
    for (const s of d.stars) {
      const jx = d.shake * Math.sin(d.t * 0.05 + s.i * 1.7) * 26;
      const jy = d.shake * Math.cos(d.t * 0.06 + s.i * 2.3) * 26;
      ctx.fillRect(Math.round(s.x + jx), Math.round(s.y + jy), s.z, s.z);
    }
  }

  function sparks(ctx, d) {
    for (const p of d.sparks) {
      ctx.fillStyle = `hsl(${p.hue} 90% ${45 + p.life * 40}%)`;
      const z = p.life > 0.55 ? 5 : 3;
      ctx.fillRect(Math.round(p.x), Math.round(p.y), z, z);
    }
  }

  function dreamVeil(ctx, d, x, y) {
    if (!d.dreaming) return;
    ctx.fillStyle = "rgba(96,64,176,.30)";
    ctx.fillRect(0, 0, LW, LH);
    ctx.fillStyle = "#cfc3ff";
    ctx.font = "700 22px 'Press Start 2P', ui-monospace, monospace";
    for (let i = 0; i < 3; i++) {
      const k = ((d.t / 1400 + i / 3) % 1 + 1) % 1;
      ctx.globalAlpha = Math.sin(k * Math.PI);
      ctx.font = `${12 + i * 6}px 'Press Start 2P', ui-monospace, monospace`;
      ctx.fillText("z", x + i * 22 + k * 10, y - i * 26 - k * 30);
    }
    ctx.globalAlpha = 1;
  }

  // The panels' eye (`firmware/main/face.c:draw_eye`, `frontend/src/pet/draw.ts:drawEye`): a
  // white rounded rect, a dark pupil with a catchlight, an upper lid tilted by `ua` and a lower
  // lid bent into the cheek raise that makes "happy" read. Lids are drawn in `lid`.
  function panelEye(ctx, cx, cy, s, p, d, side, dark, lid) {
    const open = d.open;
    const w0 = 52 * p.sx * s * (1 + 0.2 * d.startle);
    const h = 62 * p.sy * s * open * (1 + 0.2 * d.startle);
    const bw = w0 * (1 + 0.35 * Math.max(0, 1 - Math.min(open, 1)));
    if (h < 6 * s) {
      ctx.fillStyle = dark;
      rr(ctx, cx - bw / 2, cy - 2.5 * s, bw, 5 * s, 2.5 * s);
      ctx.fill();
      return;
    }
    ctx.save();
    ctx.fillStyle = "#f6f9fc";
    rr(ctx, cx - bw / 2, cy - h / 2, bw, h, Math.min(bw, h) * 0.38);
    ctx.fill();
    ctx.clip();
    const pr = Math.min(h / 2, 17 * s * (1 - 0.3 * d.startle));
    const px = cx + d.pupil.x * 11 * s;
    const py = cy + d.pupil.y * 9 * s;
    ctx.fillStyle = dark;
    ctx.beginPath();
    ctx.arc(px, py, pr, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,.92)";
    ctx.beginPath();
    ctx.arc(px - 6 * s, py - 7 * s, 5 * s, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = lid;
    const ua = p.ua;
    if (p.uy > 0.01) {
      ctx.save();
      ctx.translate(cx, cy - h / 2);
      ctx.rotate((ua * Math.PI) / 180);
      ctx.fillRect(-bw, -h, bw * 2, h + h * p.uy);
      ctx.restore();
    }
    if (p.ly > 0.01) {
      const ly = cy + h / 2 - h * p.ly;
      ctx.beginPath();
      ctx.moveTo(cx - bw, cy + h / 2 + 2);
      ctx.lineTo(cx - bw, ly);
      ctx.quadraticCurveTo(cx, ly - h * p.lb, cx + bw, ly);
      ctx.lineTo(cx + bw, cy + h / 2 + 2);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  // ---------------------------------------------------------------- A: the panels' ostrich
  // The bird from `docs/mocks/room-endpoint/ostrich-mock.py` / `face.c:draw_ostrich`, at its own
  // proportions (head 128x98, eyes +-31, the four-segment beak whose lower three drop to talk),
  // cropped to head and neck: GrowBot already has real legs, so the screen is the bust.
  function drawOstrich(ctx, d) {
    sky(ctx, d, 0.6);
    const hex = d.color;
    const s = 2.2;
    const col = hex;
    const head = shade(hex, 0.94);
    const beak = shade(hex, 0.52);
    const dark = shade(hex, 0.22);
    const cx = LW / 2 + d.gaze.x * 8;
    const hy = 250 + d.bob + d.gaze.y * 6;

    birdBody(ctx, hex, d);
    rings(ctx, shade(hex, 0.76), LW / 2, 598, cx, hy + 42 * s, 50 * s * 0.95, 34 * s * 0.95, 7, 22);

    ctx.save();
    ctx.translate(cx, hy);
    ctx.rotate((d.tilt * Math.PI) / 180);
    const perk = d.crest;
    for (const [deg, len] of [
      [-18, 42],
      [0, 50],
      [18, 42],
    ]) {
      ctx.save();
      ctx.translate(0, -98 * s * 0.5 + 6);
      ctx.rotate(((deg * (1 + perk * 0.6) + Math.sin(d.t / 420 + deg) * 2 * d.energy) * Math.PI) / 180);
      ctx.fillStyle = col;
      rr(ctx, -9 * s * 0.5, -len * s * (0.85 + perk * 0.2), 9 * s, len * s * (0.85 + perk * 0.2), 4.5 * s);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(0, -len * s * (0.85 + perk * 0.2), 9 * s * 0.62, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
    const HW = 128 * s;
    const HH = 98 * s;
    ctx.fillStyle = head;
    rr(ctx, -HW / 2, -HH / 2, HW, HH, 44 * s);
    ctx.fill();
    if (d.blush > 0.01) {
      ctx.fillStyle = `rgba(255,120,170,${0.45 * d.blush})`;
      for (const k of [-1, 1]) {
        ctx.beginPath();
        ctx.ellipse(k * 50 * s, 22 * s, 13 * s, 8 * s, 0, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    const ey = -7 * s;
    panelEye(ctx, -31 * s, ey, s * 0.86, d.lids.l, d, 1, dark, "#000");
    panelEye(ctx, 31 * s, ey, s * 0.86, d.lids.r, d, -1, dark, "#000");
    const gape = d.talk * 30;
    const BEAK = [
      [42, 13, -112],
      [33, 12, -101],
      [23, 11, -91],
      [13, 10, -82],
    ];
    if (gape > 1) {
      ctx.fillStyle = shade(hex, 0.12);
      rr(ctx, -17 * s, (-99 + 137) * s, 34 * s, (gape * 0.9 + 6) * s, 6 * s);
      ctx.fill();
    }
    ctx.fillStyle = beak;
    BEAK.forEach(([w, h, y], i) => {
      const drop = i >= 1 ? (gape * i) / 3 : 0;
      rr(ctx, (-w / 2) * s, (y + 137 + drop) * s, w * s, h * s, 5 * s);
      ctx.fill();
    });
    ctx.fillStyle = shade(hex, 0.28);
    ctx.fillRect(-16 * s, (-97 + 137) * s, 32 * s, 2 * s);
    ctx.restore();

    sparks(ctx, d);
    dreamVeil(ctx, d, cx + 120, hy - 140);
  }

  // ---------------------------------------------------------------- B: the panels' robot
  // The robot head from `face.c:draw_robot` (216x176, radius 40, antenna and bulb, eyes at
  // +-0.43 of the half-width, the smile that a gape opens out of), filling the phone, with the
  // shoulders and chest plate cropped in at the bottom.
  function drawRobot(ctx, d) {
    sky(ctx, d, 0.6);
    const hex = d.color;
    const s = 1.45;
    const col = hex;
    const dark = shade(hex, 0.22);
    const torso = shade(hex, 0.88);
    const plate = shade(hex, 0.55);
    const cx = LW / 2 + d.gaze.x * 6;
    const hy = 300 + d.bob + d.gaze.y * 5;

    ctx.fillStyle = torso;
    rr(ctx, LW / 2 - 144 * s * 0.9, 560, 288 * s * 0.9, 300, 70);
    ctx.fill();
    ctx.fillStyle = plate;
    rr(ctx, LW / 2 - 40 * s, 610, 80 * s, 60 * s, 16 * s);
    ctx.fill();
    // The chest light listens: it is the one mark that says the ear is open.
    ctx.fillStyle = d.listening ? d.glow : shade(hex, 0.4);
    ctx.beginPath();
    ctx.arc(LW / 2, 610 + 30 * s, (9 + (d.listening ? 2 * Math.sin(d.t / 180) : 0)) * s, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = shade(hex, 0.7);
    rr(ctx, LW / 2 - 34 * s, 530, 68 * s, 40, 12);
    ctx.fill();
    rings(ctx, shade(hex, 0.7), LW / 2, 532, cx, hy + 88 * s + 10, 44 * s, 38 * s, 4, 18);

    ctx.save();
    ctx.translate(cx, hy);
    ctx.rotate((d.tilt * Math.PI) / 180);
    const HW = 108 * s;
    const HH = 88 * s;
    ctx.fillStyle = col;
    ctx.fillRect(-4 * s, -HH - 30 * s, 8 * s, 30 * s);
    ctx.fillStyle = d.think ? d.glow : col;
    ctx.beginPath();
    ctx.arc(0, -HH - 36 * s, (11 + d.crest * 3) * s, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = col;
    rr(ctx, -HW, -HH, HW * 2, HH * 2, 40 * s);
    ctx.fill();
    if (d.blush > 0.01) {
      ctx.fillStyle = `rgba(255,120,170,${0.45 * d.blush})`;
      for (const k of [-1, 1]) {
        ctx.beginPath();
        ctx.ellipse(k * 78 * s, 26 * s, 16 * s, 10 * s, 0, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    const ex = HW * 0.43;
    const ey = -HH * 0.17;
    panelEye(ctx, -ex, ey, s, d.lids.l, d, 1, dark, "#000");
    panelEye(ctx, ex, ey, s, d.lids.r, d, -1, dark, "#000");

    const mx = 0;
    const my = HH * 0.52 - 16 * s;
    const mr = 30 * s;
    ctx.fillStyle = dark;
    ctx.strokeStyle = dark;
    ctx.lineCap = "round";
    ctx.lineWidth = 9 * s;
    if (d.mouth === "o") {
      ctx.beginPath();
      ctx.ellipse(mx, my + 18 * s, 18 * s, (20 + d.talk * 10) * s, 0, 0, Math.PI * 2);
      ctx.fill();
    } else {
      const bend = d.mouth === "flat" ? 0.08 : d.mouth === "small" ? 0.6 : 1;
      const gape = d.talk * 26 * s;
      const xmax = Math.cos(Math.PI * 0.15) * mr;
      const lip = (dx) => my + Math.sqrt(mr * mr - dx * dx) * bend + (1 - bend) * mr * 0.8;
      if (gape > 0.5) {
        ctx.beginPath();
        for (let dx = -xmax; dx <= xmax; dx += 2) ctx.lineTo(mx + dx, lip(dx));
        for (let dx = xmax; dx >= -xmax; dx -= 2) {
          const t = dx / xmax;
          ctx.lineTo(mx + dx, lip(dx) - gape * Math.sqrt(Math.max(0, 1 - t * t)));
        }
        ctx.closePath();
        ctx.fill();
      }
      ctx.beginPath();
      for (let dx = -xmax; dx <= xmax; dx += 2) ctx.lineTo(mx + dx, lip(dx));
      ctx.stroke();
    }
    ctx.restore();

    sparks(ctx, d);
    dreamVeil(ctx, d, cx + 110, hy - 150);
  }

  // ---------------------------------------------------------------- C: lantern eyes
  // No head: the whole phone is the face, as today, but at full resolution. Two big glowing
  // eyes in the mood's own colour carry every emotion by SHAPE (lids cut from black, the cheek
  // raise, the squint); the pupil appears only when it has a job (thinking, looking, a glance
  // at a touch). A thin light under them is the voice: it moves only while it speaks.
  let _layer = null;
  function layer(ctx) {
    if (!_layer) _layer = document.createElement("canvas");
    if (_layer.width !== ctx.canvas.width || _layer.height !== ctx.canvas.height) {
      _layer.width = ctx.canvas.width;
      _layer.height = ctx.canvas.height;
    }
    return _layer;
  }

  function drawLantern(ctx, d) {
    sky(ctx, d, 1);
    const glow = d.glow;
    const cx = LW / 2 + d.gaze.x * 22;
    const cy = 300 + d.bob + d.gaze.y * 16;
    const gap = 74;
    // The eyes are drawn on their own layer, lids CUT OUT of it, and the layer is then glowed
    // as one image: a halo drawn per shape would also glow around the lidded part, which reads
    // as a ghost of the eye the lid is hiding.
    const off = layer(ctx);
    const o = off.getContext("2d");
    o.setTransform(ctx.getTransform());
    o.clearRect(0, 0, LW, LH);
    o.translate(cx, cy);
    o.rotate(((d.tilt * Math.PI) / 180) * 0.6);
    for (const side of [-1, 1]) {
      const p = side < 0 ? d.lids.l : d.lids.r;
      const w = 104 * p.sx * (1 + 0.15 * d.startle);
      const h = 150 * p.sy * (1 + 0.15 * d.startle) * d.open;
      const bw = w * (1 + 0.3 * Math.max(0, 1 - Math.min(d.open, 1)));
      const x = side * gap;
      o.globalCompositeOperation = "source-over";
      o.fillStyle = glow;
      if (h < 12) {
        rr(o, x - bw / 2, -5, bw, 10, 5);
        o.fill();
        continue;
      }
      rr(o, x - bw / 2, -h / 2, bw, h, Math.min(bw, h) * 0.42);
      o.fill();
      o.globalCompositeOperation = "source-atop";
      o.fillStyle = "rgba(255,255,255,.35)";
      rr(o, x - bw * 0.28, -h / 2 + h * 0.12, bw * 0.2, h * 0.22, bw * 0.1);
      o.fill();
      if (d.showPupil) {
        o.fillStyle = "rgba(0,0,0,.78)";
        o.beginPath();
        o.arc(x + d.pupil.x * bw * 0.22, d.pupil.y * h * 0.2, Math.min(bw, h) * 0.17, 0, Math.PI * 2);
        o.fill();
      }
      o.globalCompositeOperation = "destination-out";
      o.fillStyle = "#000";
      if (p.uy > 0.01) {
        o.save();
        o.translate(x, -h / 2);
        o.rotate((p.ua * Math.PI) / 180);
        o.fillRect(-bw, -h, bw * 2, h + h * p.uy);
        o.restore();
      }
      if (p.ly > 0.01) {
        // A glowing eye has no pupil to hide, so the cheek raise is cut deeper than the
        // panel's: at full "happy" what is left is the upward crescent.
        const edge = h / 2 - h * p.ly * (1 - p.lb * 2);
        const peak = h / 2 - h * p.ly - h * p.lb * 1.2;
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
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.shadowColor = glow;
    ctx.shadowBlur = (30 + 18 * d.energy) * (ctx.canvas.width / LW);
    ctx.drawImage(off, 0, 0);
    ctx.restore();

    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(((d.tilt * Math.PI) / 180) * 0.6);
    // The voice line: flat and dim at rest, a moving wave while speaking.
    ctx.save();
    ctx.shadowColor = glow;
    ctx.shadowBlur = 18;
    ctx.strokeStyle = glow;
    ctx.globalAlpha = d.talking ? 0.95 : 0.25 + 0.15 * Math.max(0, d.smile);
    ctx.lineWidth = 6;
    ctx.lineCap = "round";
    ctx.beginPath();
    const mw = 70;
    const my = 150;
    const curve = d.talking ? 0 : Math.max(-0.6, Math.min(1, d.smile)) * 16;
    for (let i = 0; i <= 40; i++) {
      const u = i / 40;
      const xx = -mw + u * mw * 2;
      const env = Math.sin(u * Math.PI);
      const wave = d.talking ? Math.sin(u * 14 + d.t / 70) * d.talk * 22 * env : 0;
      ctx.lineTo(xx, my + wave + curve * env);
    }
    ctx.stroke();
    ctx.restore();
    ctx.restore();

    sparks(ctx, d);
    dreamVeil(ctx, d, cx + 120, cy - 150);
  }

  root.GROWBOT_FACES = {
    LW,
    LH,
    ostrich: { name: "Ostrich", draw: drawOstrich, tinted: true },
    robot: { name: "Robot", draw: drawRobot, tinted: true },
    lantern: { name: "Lantern", draw: drawLantern, tinted: false },
  };
})(window);
