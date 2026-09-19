// Physical geometry of the Waveshare ESP32-S3-Touch-AMOLED-1.8, and the scaling maths that
// makes the preview honest about it.
//
// The panel is 368x448 over 29.02 x 35.33 mm — 322 ppi, a smartwatch face. Showing it at a
// believable size is the whole point of the preview: the binding interaction decision (one
// whole-screen touch target, because a 20 mm child target is 69% x 57% of this panel) is only
// checkable against the real millimetres.
//
// CSS cannot deliver them. `1in` is anchored to exactly 96px whatever the display is, so
// `width: 29.02mm` is a physical measurement only on a ~96 CSS-ppi screen — a desktop monitor.
// A phone runs ~150-160 CSS ppi, where the same rule lays down 96 CSS px per inch on a screen
// that fits ~153 into one, drawing the panel at roughly 96/153 — about two-thirds of its real
// size. Claiming a size and being out by a third is worse than claiming none, and no API
// reports the screen's real pitch.
//
// So the viewer measures it, once, against an object whose size is fixed by standard.

/** Panel dimensions in millimetres (Waveshare ESP32-S3-Touch-AMOLED-1.8, 1.8" at 368x448). */
export const PANEL_MM_W = 29.02;
export const PANEL_MM_H = 35.33;

/** ISO/IEC 7810 ID-1 width: every credit, debit, and driving-licence card on earth, to 0.1 mm. */
export const CARD_MM_W = 85.6;

/** CSS's own answer (1in === 96px), which is right on a ~96 ppi monitor and the honest place
 * to start the slider — it is a guess about the screen, not a measurement of it. */
export const NOMINAL_CARD_PX = (CARD_MM_W * 96) / 25.4;

/** Slider bounds, wide enough for a 4K desktop panel at one end and a dense phone at the other. */
export const CARD_PX_MIN = 140;
export const CARD_PX_MAX = 900;

const STORE_KEY = "jbrain.petface.cardPx";

/** How the stage is sized. Three different questions, and no one size answers more than one:
 *  - `fit`    — big enough to see what you are drawing. The working view.
 *  - `pixels` — 368x448 DEVICE pixels, so the render is as crisp as the panel is, no finer.
 *  - `actual` — the real millimetres, from the viewer's calibration. Needs one. */
export type StageMode = "fit" | "pixels" | "actual";

export const STAGE_MODES: readonly StageMode[] = ["fit", "pixels", "actual"];

export interface StageSize {
  /** CSS pixels. */
  w: number;
  h: number;
}

/** CSS pixels per millimetre implied by a calibration. */
export function pxPerMm(cardPx: number): number {
  return cardPx / CARD_MM_W;
}

/** CSS pixels per physical inch — the number that says what the screen actually is, and the
 * one worth showing back: a phone reading ~96 means the viewer has not calibrated yet. */
export function cssPpi(cardPx: number): number {
  return pxPerMm(cardPx) * 25.4;
}

export function clampCardPx(px: number): number {
  if (!Number.isFinite(px)) return NOMINAL_CARD_PX;
  return Math.min(CARD_PX_MAX, Math.max(CARD_PX_MIN, px));
}

/** The stage's CSS size for a mode. `actual` without a calibration is not guessed at — the
 * caller is expected to have sent the viewer to calibrate, so falling back to the nominal
 * would quietly restore the lie this module exists to remove. */
export function stageSize(
  mode: StageMode,
  opts: { panelW: number; panelH: number; dpr: number; cardPx: number | null },
): StageSize {
  const { panelW, panelH, dpr, cardPx } = opts;
  if (mode === "pixels") {
    const d = dpr > 0 ? dpr : 1;
    return { w: panelW / d, h: panelH / d };
  }
  if (mode === "actual" && cardPx !== null) {
    const k = pxPerMm(clampCardPx(cardPx));
    return { w: PANEL_MM_W * k, h: PANEL_MM_H * k };
  }
  return { w: panelW, h: panelH };
}

/** localStorage throws in a private window and returns nothing after a site-data clear, so
 * every read and write is guarded and an absent calibration is a normal state, not an error. */
export function loadCardPx(): number | null {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    if (raw === null) return null;
    const n = Number.parseFloat(raw);
    return Number.isFinite(n) ? clampCardPx(n) : null;
  } catch {
    return null;
  }
}

export function saveCardPx(px: number): void {
  try {
    window.localStorage.setItem(STORE_KEY, String(clampCardPx(px)));
  } catch {
    // A viewer who cannot persist still gets the calibration for this session.
  }
}

export function clearCardPx(): void {
  try {
    window.localStorage.removeItem(STORE_KEY);
  } catch {
    // Nothing to undo if storage was never reachable.
  }
}
