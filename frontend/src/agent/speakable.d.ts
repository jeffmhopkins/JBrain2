/** Which voice engine's phonemizer the utterance pass targets. piper and (today) kokoro share
 * one espeak-ng ruleset; the seam lets kokoro diverge (misaki-aware) without a re-thread. */
export type SpeakEngine = "piper" | "kokoro";

/** Structural, engine-agnostic pass: Markdown → plain multi-line prose (strip markup, linearize
 * tables/lists, drop markers, remove emphasis). Newlines preserved for the utterance pass. */
export function toProse(md: string): string;

/** Pronunciation + pacing pass over structural prose, tuned to `engine`'s phonemizer: verbalize
 * numbers/symbols/emoji, author pauses, shape dashes/parentheticals. Returns one speakable line. */
export function toUtterance(prose: string, engine?: SpeakEngine): string;

/** Normalize an assistant answer's Markdown into legible, speakable plain text for `engine`
 * (default piper) — toProse composed with toUtterance. Shared verbatim with the wall display. */
export function speakable(md: string, engine?: SpeakEngine): string;

/** Streaming, block-aware splitter: given the raw markdown received since the caller's
 * cursor, return normalized speakable clips for the complete units it can emit now, plus
 * how many raw chars were consumed (advance a raw-space cursor). Incomplete trailing blocks
 * and a partial trailing sentence are held until `flush`. */
export function chunkStream(
  raw: string,
  flush: boolean,
  engine?: SpeakEngine,
): { chunks: string[]; consumed: number };
