/** Normalize an assistant answer's Markdown into legible, speakable plain text for TTS:
 * strip markdown, linearize tables/lists, verbalize numbers/symbols/emoji, and author
 * pauses via terminal punctuation. Shared verbatim with the wall display's copy. */
export function speakable(md: string): string;
