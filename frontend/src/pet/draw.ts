// Canvas renderer for the pet, drawn at the panel's native 368x448.
//
// The size is not arbitrary and is not a phone shape: the ordered hardware is a Waveshare
// ESP32-S3-Touch-AMOLED-1.8, **368x448 in 29.0 x 35.3 mm at 322 ppi** — a smartwatch panel,
// about a postage stamp. Rendering at the real framebuffer size here means nothing in this
// surface can be achievable in the PWA but not on the device, which is the whole point of
// building the PWA first (`docs/plans/PET_ENDPOINT_PWA_PLAN.md`).
//
// Two consequences visible throughout: true black is free (AMOLED) so backgrounds are #000 and
// pixel art reads as *emitting*; and **text is a debug channel only** — the audience is
// pre-literate, so every state must survive without it.

import type { FaceParams, MouthShape } from "./face";
import type { FigurePose, RigPose } from "./rig";
import { cornerRadius } from "./scale";

export const PANEL_W = 368;
export const PANEL_H = 448;

/** The 12 shipped colours (`jpet/intents.py:PET_COLORS` → `deploy/wall/pet.html:NAMED`). */
export const NAMED: Record<string, string> = {
  cyan: "#3bf0ff",
  magenta: "#ff4fd8",
  gold: "#ffd23f",
  orange: "#ffb03a",
  blue: "#6a7bff",
  red: "#ff477e",
  green: "#49f08a",
  pink: "#ff8ad0",
  purple: "#b06aff",
  white: "#ffffff",
};
export const COLOR_NAMES = [...Object.keys(NAMED), "rainbow", "default"];

/** The 7 shipped creature forms (`jpet/intents.py:FORMS`). The face underneath is identical for
 *  all of them — only the silhouette changes, which is what makes one face plus seven hats
 *  cheap enough for the panel. */
export const FORMS = ["robot", "dog", "cat", "dragon", "cow", "pig", "chicken"] as const;
export type Form = (typeof FORMS)[number];

/** The form's own colour when the pet has no override. `ROBOT_BLUE` is the last-resort
 *  fallback so an unknown form from a newer server still draws something. */
const ROBOT_BLUE = "#7FA7C9";
const FORM_DEFAULT: Record<string, string> = {
  robot: "#7FA7C9",
  dog: "#c68a4a",
  cat: "#9aa0b0",
  dragon: "#49f08a",
  cow: "#f2f2fa",
  pig: "#ff9ad8",
  chicken: "#fff09e",
};

// Figure geometry, in figure-space. Set by the panel rather than by taste: at 368x448 the head
// is 216x176 and the whole creature fits with margin at the top of a jump, in every form.
const HEAD_Y = -96;
const HW = 108;
const HH = 88;
const SHOULDER_Y = 6;
const HIP_Y = 98;
const ARM_L = 74;
const LEG_L = 54;
const FIGURE_SCALE = 0.84;

export interface Scene {
  face: FaceParams;
  rig: RigPose;
  figure: FigurePose;
  form: string;
  /** A named colour, "rainbow", or null for the form's own default. */
  color: string | null;
  blink: boolean;
  gaze: { x: number; y: number };
  /** Milliseconds since start — drives rainbow cycling and idle motion. */
  t: number;
  /** Mic open. Drawn as a whole-panel ring because the audience cannot read a caption, and
   *  the ICO Children's Code requires an unambiguous recording indicator. */
  listening: boolean;
  /** Debug only. Never shown to the child in anger. */
  /** Debug only. Explicitly `| undefined` because the project runs `exactOptionalPropertyTypes`
   *  and the caller composes this field conditionally. */
  caption?: string | undefined;
}

export function bodyColor(color: string | null, form: string, t: number): string {
  if (color === "rainbow") return `hsl(${(t * 0.05) % 360} 85% 62%)`;
  if (!color || color === "default") return FORM_DEFAULT[form] ?? ROBOT_BLUE;
  return NAMED[color] ?? FORM_DEFAULT[form] ?? ROBOT_BLUE;
}

/** Darken a hex colour. `hsl(...)` (rainbow) passes through — it is already a live value and
 *  shading it per-frame would strobe. */
export function shade(hex: string, f: number): string {
  if (!hex.startsWith("#")) return hex;
  const n = Number.parseInt(hex.slice(1), 16);
  const r = Math.round(((n >> 16) & 255) * f);
  const g = Math.round(((n >> 8) & 255) * f);
  const b = Math.round((n & 255) * f);
  return `rgb(${r},${g},${b})`;
}

function limbPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  ang: number,
  len: number,
  w: number,
  col: string,
  hand: boolean,
): void {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate((ang * Math.PI) / 180);
  ctx.fillStyle = col;
  ctx.beginPath();
  ctx.roundRect(-w / 2, 0, w, len, w / 2);
  ctx.fill();
  if (hand) {
    ctx.beginPath();
    ctx.arc(0, len, w * 0.62, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}

function drawEye(
  ctx: CanvasRenderingContext2D,
  cx: number,
  cy: number,
  p: FaceParams["l"],
  dark: string,
  gx: number,
  gy: number,
  blink: boolean,
): void {
  // The eye WIDENS as it closes — squash-and-stretch on a blink, straight from Cozmo.
  const w = 52 * p.sx;
  const h = (blink ? 0.06 : 1) * 62 * p.sy;
  const bw = blink ? w * 1.35 : w;
  ctx.save();
  ctx.fillStyle = "#f6f9fc";
  ctx.beginPath();
  ctx.roundRect(cx - bw / 2, cy - h / 2, bw, h, Math.min(bw, h) * 0.38);
  ctx.fill();
  if (!blink) {
    ctx.save();
    ctx.beginPath();
    ctx.roundRect(cx - bw / 2, cy - h / 2, bw, h, Math.min(bw, h) * 0.38);
    ctx.clip();
    ctx.fillStyle = dark;
    ctx.beginPath();
    ctx.arc(cx + gx, cy + gy, 17, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,.9)"; // catchlight — cheap, disproportionate effect
    ctx.beginPath();
    ctx.arc(cx + gx - 6, cy + gy - 7, 5, 0, Math.PI * 2);
    ctx.fill();
    if (p.uy > 0.01) {
      ctx.save();
      ctx.translate(cx, cy - h / 2);
      ctx.rotate((p.ua * Math.PI) / 180);
      ctx.fillStyle = "#000";
      ctx.fillRect(-bw, 0, bw * 2, h * p.uy);
      ctx.restore();
    }
    if (p.ly > 0.01) {
      // The lower lid / cheek arc: the Duchenne raise, and what makes "happy" read at all.
      ctx.fillStyle = "#000";
      ctx.beginPath();
      const ly = cy + h / 2 - h * p.ly;
      const bend = h * p.lb;
      ctx.moveTo(cx - bw, cy + h / 2 + 2);
      ctx.lineTo(cx - bw, ly);
      ctx.quadraticCurveTo(cx, ly - bend, cx + bw, ly);
      ctx.lineTo(cx + bw, cy + h / 2 + 2);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }
  ctx.restore();
}

function drawMouth(
  ctx: CanvasRenderingContext2D,
  cx: number,
  cy: number,
  kind: MouthShape,
  dark: string,
): void {
  ctx.strokeStyle = dark;
  ctx.lineWidth = 9;
  ctx.lineCap = "round";
  ctx.beginPath();
  if (kind === "smile") {
    ctx.arc(cx, cy - 16, 30, Math.PI * 0.15, Math.PI * 0.85);
    ctx.stroke();
  } else if (kind === "open" || kind === "o") {
    ctx.fillStyle = dark;
    ctx.beginPath();
    ctx.ellipse(cx, cy, kind === "o" ? 20 : 28, kind === "o" ? 26 : 20, 0, 0, Math.PI * 2);
    ctx.fill();
  } else if (kind === "small") {
    ctx.arc(cx, cy - 8, 14, Math.PI * 0.15, Math.PI * 0.85);
    ctx.stroke();
  } else if (kind === "flat") {
    ctx.moveTo(cx - 22, cy);
    ctx.lineTo(cx + 22, cy);
    ctx.stroke();
  } else {
    ctx.arc(cx, cy - 14, 26, Math.PI * 0.1, Math.PI * 0.9);
    ctx.stroke();
    ctx.fillStyle = "#ff8ad0";
    ctx.beginPath();
    ctx.roundRect(cx - 12, cy + 2, 24, 26, 11);
    ctx.fill();
  }
}

/** Ears, horns, beak — the silhouette is the only thing that tells the seven forms apart. */
function drawSilhouette(
  ctx: CanvasRenderingContext2D,
  hh: number,
  form: string,
  col: string,
  dark: string,
): void {
  ctx.fillStyle = col;
  if (form === "robot") {
    ctx.fillRect(-4, -hh - 30, 8, 30);
    ctx.beginPath();
    ctx.arc(0, -hh - 36, 11, 0, Math.PI * 2);
    ctx.fill();
  } else if (form === "dog") {
    for (const s of [-1, 1]) {
      ctx.beginPath();
      ctx.roundRect(s * HW - (s < 0 ? 26 : 2), -hh + 8, 28, 96, 16);
      ctx.fill();
    }
  } else if (form === "cat") {
    for (const s of [-1, 1]) {
      ctx.beginPath();
      ctx.moveTo(s * 70, -hh + 16);
      ctx.lineTo(s * 104, -hh - 56);
      ctx.lineTo(s * 126, -hh + 24);
      ctx.closePath();
      ctx.fill();
    }
  } else if (form === "dragon" || form === "cow") {
    ctx.fillStyle = form === "cow" ? "#f7f7fb" : col;
    for (const s of [-1, 1]) {
      ctx.beginPath();
      ctx.moveTo(s * 78, -hh + 14);
      ctx.lineTo(s * 116, -hh - 50);
      ctx.lineTo(s * 100, -hh + 22);
      ctx.closePath();
      ctx.fill();
    }
  } else if (form === "pig") {
    ctx.fillStyle = shade(col, 0.85);
    ctx.beginPath();
    ctx.ellipse(0, 58, 30, 22, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = dark;
    for (const s of [-1, 1]) {
      ctx.beginPath();
      ctx.arc(s * 11, 58, 5, 0, Math.PI * 2);
      ctx.fill();
    }
  } else if (form === "chicken") {
    ctx.fillStyle = "#ff5a4e";
    for (let i = -1; i < 2; i++) {
      ctx.beginPath();
      ctx.arc(i * 22, -hh - 14, 15, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.fillStyle = "#ffb03a";
    ctx.beginPath();
    ctx.moveTo(-20, 52);
    ctx.lineTo(20, 52);
    ctx.lineTo(0, 82);
    ctx.closePath();
    ctx.fill();
  }
  if (form === "cow") {
    ctx.fillStyle = shade(col, 0.72);
    ctx.beginPath();
    ctx.ellipse(-72, 40, 26, 20, 0.4, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(80, -46, 20, 16, -0.3, 0, Math.PI * 2);
    ctx.fill();
  }
}

/** The visible display area: the panel rectangle rounded by the case, inset by `pad`. */
function roundedPanelPath(ctx: CanvasRenderingContext2D, pad = 0): void {
  const r = Math.max(0, cornerRadius(PANEL_W) - pad);
  ctx.beginPath();
  ctx.roundRect(pad, pad, PANEL_W - pad * 2, PANEL_H - pad * 2, r);
}

export function drawScene(ctx: CanvasRenderingContext2D, s: Scene): void {
  const col = bodyColor(s.color, s.form, s.t);
  const dark = shade(col, 0.22);
  const limb = shade(col, 0.78);
  const torso = shade(col, 0.88);

  // The assembled unit's case rounds the display into a squircle (photographed
  // 2026-09-19), so the rectangle's corners are not visible to anyone holding it. Clipping
  // to that shape is the same class of honesty as the calibrated size: a preview that
  // draws pixels the case hides invites a design that loses them on the desk.
  //
  // The corners are left TRANSPARENT rather than filled black. On an AMOLED black and off
  // are the same thing, so a black corner would say "this part of the display is dark"
  // when the truth is "there is no display here" — and the two look identical until
  // someone puts a caption in one.
  ctx.clearRect(0, 0, PANEL_W, PANEL_H);
  ctx.save();
  roundedPanelPath(ctx);
  ctx.clip();

  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, PANEL_W, PANEL_H);

  if (s.listening) {
    const q = 0.5 + 0.5 * Math.sin(s.t / 240);
    ctx.strokeStyle = `rgba(143,188,154,${0.35 + 0.35 * q})`;
    ctx.lineWidth = 10;
    // Traces the case's own curve rather than a rectangle, so the listening ring reads as
    // a rim light around the bezel instead of a box with clipped corners.
    roundedPanelPath(ctx, 5);
    ctx.stroke();
  }

  ctx.save();
  ctx.translate(PANEL_W / 2 + s.figure.ox, PANEL_H * 0.545 + s.figure.oy);
  ctx.rotate((s.figure.ang * Math.PI) / 180);
  ctx.scale(s.figure.sx, s.figure.sy);
  ctx.scale(FIGURE_SCALE, FIGURE_SCALE);

  for (const [side, ang] of [
    [-1, s.rig.legL],
    [1, s.rig.legR],
  ] as const) {
    limbPath(ctx, side * 34, HIP_Y, ang, LEG_L, 30, limb, true);
  }
  if (!s.rig.handsUp) {
    for (const [side, ang] of [
      [-1, s.rig.armL],
      [1, s.rig.armR],
    ] as const) {
      limbPath(ctx, side * 66, SHOULDER_Y, ang, ARM_L, 28, limb, true);
    }
  }
  ctx.fillStyle = torso;
  ctx.beginPath();
  ctx.roundRect(-72, -26, 144, 132, 40);
  ctx.fill();
  if (s.form === "robot") {
    ctx.fillStyle = shade(col, 0.55);
    ctx.beginPath();
    ctx.roundRect(-26, 10, 52, 40, 12);
    ctx.fill();
  }

  ctx.save();
  ctx.translate(0, HEAD_Y);
  drawSilhouette(ctx, HH, s.form, col, dark);
  ctx.fillStyle = col;
  ctx.beginPath();
  ctx.roundRect(-HW, -HH, HW * 2, HH * 2, s.form === "robot" ? 40 : 74);
  ctx.fill();
  if (s.figure.extra === "blush") {
    ctx.fillStyle = "rgba(255,120,160,.5)";
    for (const side of [-1, 1]) {
      ctx.beginPath();
      ctx.ellipse(side * (HW * 0.7), HH * 0.34, 22, 14, 0, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  const eyeY = -HH * 0.17;
  const eyeX = HW * 0.43;
  const eyeScale = 0.78;
  ctx.save();
  ctx.translate(0, eyeY);
  ctx.scale(eyeScale, eyeScale);
  drawEye(ctx, -eyeX / eyeScale, 0, s.face.l, dark, s.gaze.x, s.gaze.y, s.blink);
  drawEye(ctx, eyeX / eyeScale, 0, s.face.r, dark, s.gaze.x, s.gaze.y, s.blink);
  ctx.restore();
  drawMouth(ctx, 0, HH * 0.52, s.face.mouth, dark);
  ctx.restore();

  if (s.rig.handsUp) {
    // Peekaboo. Don't pose an angle — SOLVE for the eye, then cover it with a PALM. A fixed
    // angle leaves the hand short of the eye, and the limb's own hand knob (29 px) is smaller
    // than the widest eye (43 px), so either mistake reads as "arms up beside the head".
    const lift = s.rig.handsUp;
    for (const side of [-1, 1]) {
      const sx = side * 66;
      const dx = side * eyeX - sx;
      const dy = HEAD_Y + eyeY - SHOULDER_Y;
      const aim = (Math.atan2(dx, dy) * 180) / Math.PI;
      const reach = Math.hypot(dx, dy);
      const rest = side * -12;
      limbPath(
        ctx,
        sx,
        SHOULDER_Y,
        rest + (aim - rest) * lift,
        ARM_L + (reach - ARM_L) * lift,
        28,
        limb,
        false,
      );
      ctx.save();
      ctx.globalAlpha = lift;
      ctx.fillStyle = limb;
      ctx.beginPath();
      ctx.arc(side * eyeX, HEAD_Y + eyeY, 38, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }
  ctx.restore();

  if (s.figure.extra === "puff") {
    for (let i = 0; i < 6; i++) {
      const q = ((s.t / 900) * 1.6 + i / 6) % 1;
      ctx.fillStyle = `rgba(140,200,150,${(1 - q) * 0.5})`;
      ctx.beginPath();
      ctx.arc(
        PANEL_W / 2 + 62 + i * 15,
        PANEL_H * 0.545 + 104 - q * 58,
        7 + q * 16,
        0,
        Math.PI * 2,
      );
      ctx.fill();
    }
  }
  if (s.caption) {
    ctx.fillStyle = "rgba(230,231,233,.32)";
    ctx.textAlign = "center";
    ctx.font = "400 19px system-ui";
    ctx.fillText(s.caption, PANEL_W / 2, PANEL_H - 18);
  }

  // Ends the case-shape clip opened at the top.
  ctx.restore();
}
