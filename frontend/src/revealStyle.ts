// How the Full Brain transcript reveals each finished line of a streamed answer
// (agent/markdown.tsx). The reveal is gated to completed blocks either way — a
// formula only appears once fully typeset, never mid-render — this just picks the
// motion: "instant" snaps the line in, "cascade" fades its words in left-to-right,
// "sweep" wipes the whole line in left-to-right. Device-local, like theme and text
// size; the worker never sees it.

export type RevealStyle = "instant" | "cascade" | "sweep";

const STORAGE_KEY = "jbrain.revealStyle";
const DEFAULT_STYLE: RevealStyle = "sweep";
export const REVEAL_STYLES: RevealStyle[] = ["instant", "cascade", "sweep"];

export function getRevealStyle(): RevealStyle {
  const raw = localStorage.getItem(STORAGE_KEY);
  return raw && (REVEAL_STYLES as string[]).includes(raw) ? (raw as RevealStyle) : DEFAULT_STYLE;
}

export function setRevealStyle(style: RevealStyle): void {
  localStorage.setItem(STORAGE_KEY, style);
}
