// FTS snippets carry literal <mark>…</mark> around keyword hits. The UI
// renders them by splitting into typed segments — never via innerHTML — so
// any other markup in a snippet stays inert text.

export interface SnippetSegment {
  text: string;
  marked: boolean;
}

export function splitMarks(snippet: string): SnippetSegment[] {
  const segments: SnippetSegment[] = [];
  let rest = snippet;
  while (rest.length > 0) {
    const open = rest.indexOf("<mark>");
    if (open < 0) {
      segments.push({ text: rest, marked: false });
      break;
    }
    if (open > 0) segments.push({ text: rest.slice(0, open), marked: false });
    const afterOpen = rest.slice(open + "<mark>".length);
    const close = afterOpen.indexOf("</mark>");
    if (close < 0) {
      // Unterminated mark — treat the remainder as highlighted.
      if (afterOpen.length > 0) segments.push({ text: afterOpen, marked: true });
      break;
    }
    if (close > 0) segments.push({ text: afterOpen.slice(0, close), marked: true });
    rest = afterOpen.slice(close + "</mark>".length);
  }
  return segments;
}
