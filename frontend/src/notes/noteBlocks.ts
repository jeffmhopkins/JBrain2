// A composed note's text, split back into the author's body and the dated blocks
// appended after it (`backend/src/jbrain/notes/compose.py`).
//
// D6 keeps a note's body FROZEN and appends timestamped clarification blocks to its
// text, as a storage decision. Storage was all it ever was: every surface rendered the
// composed string verbatim, so the owner read his own note as
//
//     My tv is 58"
//
//     [addition 2026-09-15 01:24 UTC]
//     Actually it's 60"
//
// — machine syntax, in the two lines a clamped stream row has to spend on content, with
// the words he actually typed cut off after it. He reported it as the additions
// "showing like poop".
//
// So the marker is parsed here and rendered by each surface in its own register: the
// stream row spends its clamp on the text, the note screen dates the block properly.
// The BACKEND format stays exactly as it is — it is what the next reading reads, and
// "the agent asked and he answered" versus "he came back and said this" is a real
// distinction in that text (`compose.clarification_block`). This is the reader.

/** One appended block: what kind it is, when it landed, and the words. */
export interface NoteBlock {
  kind: "addition" | "clarification";
  /** The stamp exactly as composed ("2026-09-15 01:24 UTC") — not re-parsed into a
   * Date, because it is already the string the backend chose to show and re-formatting
   * it in the browser's locale would silently move the instant. */
  at: string;
  /** Present only on a clarification: the question the agent had asked. */
  question?: string;
  answer: string;
}

export interface ParsedNote {
  /** The author's own body — byte-identical to what he wrote. */
  body: string;
  blocks: NoteBlock[];
}

// Anchored at a line start, and the stamp shape is pinned rather than `.*`: the marker
// is ordinary prose the moment it is not (a note quoting a clarified note is a real
// case, which is why `compose.strip_clarifications` refuses to cut on the literal).
// Matching the STAMP too is what keeps a pasted "[addition ...]" line from being read
// as structure.
const MARK = /^\[(addition|clarification) (\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)\]$/;

/** Split composed note text. A note with no blocks returns its body unchanged, so every
 * caller can parse unconditionally. */
export function parseNote(text: string): ParsedNote {
  const lines = text.split("\n");
  const starts: number[] = [];
  for (let i = 0; i < lines.length; i++) {
    if (MARK.test(lines[i] as string)) starts.push(i);
  }
  if (starts.length === 0) return { body: text, blocks: [] };

  const blocks: NoteBlock[] = [];
  for (let b = 0; b < starts.length; b++) {
    const at = starts[b] as number;
    const end = b + 1 < starts.length ? (starts[b + 1] as number) : lines.length;
    const m = MARK.exec(lines[at] as string);
    if (m === null) continue;
    // The block's own lines, minus the blank line that separates it from the next.
    const rest = lines.slice(at + 1, end);
    while (rest.length > 0 && (rest[rest.length - 1] as string).trim() === "") rest.pop();
    const kind = m[1] as NoteBlock["kind"];
    if (kind === "clarification") {
      // `Q:` / `A:` as `clarification_block` writes them. A question can wrap, so the
      // split is on the `A:` line rather than on line one and line two.
      const aAt = rest.findIndex((l) => l.startsWith("A: "));
      const q = (aAt === -1 ? rest : rest.slice(0, aAt)).join("\n").replace(/^Q: /, "").trim();
      const a = aAt === -1 ? "" : rest.slice(aAt).join("\n").replace(/^A: /, "").trim();
      blocks.push({ kind, at: m[2] as string, question: q, answer: a });
    } else {
      blocks.push({ kind, at: m[2] as string, answer: rest.join("\n").trim() });
    }
  }
  // Everything before the first marker, minus the blank line `composed_suffix` inserted.
  const body = lines
    .slice(0, starts[0] as number)
    .join("\n")
    .replace(/\n+$/, "");
  return { body, blocks };
}

/** The note as CONTINUOUS PROSE — body then every block's words, markers gone.
 *
 * What a clamped preview should spend its two lines on. The stamps are dropped rather
 * than shortened: in a two-line preview the newest thing he typed is worth more than
 * when he typed it, and the note screen still dates every block properly. */
export function previewText(text: string): string {
  const { body, blocks } = parseNote(text);
  if (blocks.length === 0) return body;
  return [body, ...blocks.map((b) => b.answer)].filter((s) => s !== "").join("\n");
}
