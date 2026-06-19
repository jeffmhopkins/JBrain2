// Typewriter pacing rate: the target speed, in tokens/second, at which the Full
// Brain transcript reveals streamed text. The reveal is decoupled from network
// chunk arrival (see agent/usePacedText.ts), so a fast local model (50+ t/s) reads
// as smooth typing instead of snapping in whole chunks. 0 means "instant" — pacing
// off, text shows as it arrives. Device-local, like theme and text size; the worker
// never sees it.

export type TokenRate = number; // tokens/sec; 0 = instant (no pacing)

const STORAGE_KEY = "jbrain.tokenRate";
const DEFAULT_RATE: TokenRate = 30;
export const TOKEN_RATES: TokenRate[] = [0, 20, 30, 45, 60];

export function getTokenRate(): TokenRate {
  // An absent key must fall to the default, NOT to 0 (Number(null) === 0, and 0 is
  // itself a valid "instant" value — so guard the null case explicitly).
  const raw = localStorage.getItem(STORAGE_KEY);
  if (raw === null) return DEFAULT_RATE;
  const n = Number(raw);
  return TOKEN_RATES.includes(n) ? n : DEFAULT_RATE;
}

export function setTokenRate(rate: TokenRate): void {
  localStorage.setItem(STORAGE_KEY, String(rate));
}
