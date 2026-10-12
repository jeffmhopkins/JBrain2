// Split a deep-research report into the "stories" the drive-mode player skips between
// (docs/mocks/research-drive-play/, variant C2). A report is written as `## Heading`
// sections — the daily_news briefing's six beats among them — so each level-1/2 heading
// opens a story; deeper headings stay inside their story. Text before the first heading
// (a lead paragraph) is its own untitled story.

import { reportToSpeech, speakable } from "./speakable.js";

export interface ReportSection {
  /** The heading text, markup stripped ("" for an untitled lead). */
  title: string;
  /** The story's Markdown, already prepared for speech (headings and sources dropped). */
  md: string;
  /** Rough spoken length, for the player's "~N min left" and segment widths. */
  seconds: number;
}

const HEADING = /^\s{0,3}#{1,2}\s+(.*?)\s*#*\s*$/;
const SOURCES = /^(?:sources|references|citations)\b/i;
// Kokoro's default voice at speed 1.0 reads a briefing at roughly this pace; only the
// relative widths and a "~" estimate depend on it, so it needn't be exact.
const WORDS_PER_SECOND = 2.6;

function cleanTitle(raw: string): string {
  return raw
    .replace(/[*_`]/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .trim();
}

export function reportSections(md: string): ReportSection[] {
  const parts: { title: string; lines: string[] }[] = [{ title: "", lines: [] }];
  for (const line of String(md || "").split("\n")) {
    const m = HEADING.exec(line);
    if (m) {
      const title = cleanTitle(m[1] ?? "");
      // Everything from a Sources heading on is URLs — never spoken (as reportToSpeech).
      if (SOURCES.test(title)) break;
      parts.push({ title, lines: [] });
    } else {
      (parts[parts.length - 1] as { lines: string[] }).lines.push(line);
    }
  }
  const out: ReportSection[] = [];
  for (const p of parts) {
    const body = reportToSpeech(p.lines.join("\n"));
    const spoken = speakable(body, "kokoro").trim();
    // A heading with nothing under it (or a lone `# Title` line) has nothing to say.
    if (!spoken) continue;
    const words = spoken.split(/\s+/).length;
    out.push({ title: p.title, md: body, seconds: Math.max(1, words / WORDS_PER_SECOND) });
  }
  return out;
}
