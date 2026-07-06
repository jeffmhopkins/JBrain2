/** Normalize an assistant answer's Markdown into legible, speakable plain text for TTS:
 * strip markdown, linearize tables/lists, verbalize numbers/symbols/emoji, and author
 * pauses via terminal punctuation. Shared verbatim with the wall display's copy. */
export function speakable(md: string): string;

/** Streaming, block-aware splitter: given the raw markdown received since the caller's
 * cursor, return normalized speakable clips for the complete units it can emit now, plus
 * how many raw chars were consumed (advance a raw-space cursor). Incomplete trailing blocks
 * and a partial trailing sentence are held until `flush`. */
export function chunkStream(
  raw: string,
  flush: boolean,
): { chunks: string[]; consumed: number };
