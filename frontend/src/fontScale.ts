// Text-size setting: a multiplier over the DESIGN.md type scale, applied via
// the --font-scale CSS variable. 75% is the owner-chosen default; 100% is
// the design-doc sizes as originally drawn.

export type FontScale = 65 | 75 | 90 | 100;

const STORAGE_KEY = "jbrain.fontScale";
const DEFAULT_SCALE: FontScale = 75;
export const FONT_SCALES: FontScale[] = [65, 75, 90, 100];

export function getFontScale(): FontScale {
  const stored = Number(localStorage.getItem(STORAGE_KEY));
  return FONT_SCALES.includes(stored as FontScale) ? (stored as FontScale) : DEFAULT_SCALE;
}

function apply(scale: FontScale): void {
  document.documentElement.style.setProperty("--font-scale", String(scale / 100));
}

export function setFontScale(scale: FontScale): void {
  localStorage.setItem(STORAGE_KEY, String(scale));
  apply(scale);
}

export function initFontScale(): void {
  apply(getFontScale());
}
