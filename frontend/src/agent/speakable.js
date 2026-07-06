// Turn an assistant answer's Markdown into legible, speakable plain text for TTS.
//
// This is the SINGLE source of truth for read-aloud text normalization, shared by two
// surfaces that cannot share a build: the PWA (imports it, typed via speakable.d.ts) and
// the wall display's index.html (loads a byte-identical copy at deploy/server-brain/
// speakable.js via <script src>, guarded by a parity test). Authored as plain ESM — no
// framework, no deps — so the browser runs it verbatim.
//
// piper is a plain-text neural voice: no SSML, no markup — pauses come only from
// punctuation and sentence splitting. So every legibility win is a plain-text rewrite:
// strip markdown, linearize tables/lists, verbalize numbers/symbols/emoji, and — the
// highest-leverage fix — AUTHOR pauses by making each line end in terminal punctuation
// BEFORE whitespace is collapsed (otherwise a bullet list with no periods is read as one
// breathless run).

// --- numbers → words (compact; integers to billions, decimals digit-by-digit) ---------

const ONES = [
  "zero",
  "one",
  "two",
  "three",
  "four",
  "five",
  "six",
  "seven",
  "eight",
  "nine",
  "ten",
  "eleven",
  "twelve",
  "thirteen",
  "fourteen",
  "fifteen",
  "sixteen",
  "seventeen",
  "eighteen",
  "nineteen",
];
const TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"];
const SCALES = [
  ["", 1],
  ["thousand", 1e3],
  ["million", 1e6],
  ["billion", 1e9],
];

/** A non-negative integer (< 1 trillion) to words. */
function intToWords(n) {
  if (n < 20) return ONES[n];
  if (n < 100) {
    const t = TENS[Math.floor(n / 10)];
    const o = n % 10;
    return o ? `${t} ${ONES[o]}` : t;
  }
  if (n < 1000) {
    const h = Math.floor(n / 100);
    const rest = n % 100;
    return rest ? `${ONES[h]} hundred ${intToWords(rest)}` : `${ONES[h]} hundred`;
  }
  for (let i = SCALES.length - 1; i >= 1; i--) {
    const [word, value] = SCALES[i];
    if (n >= value) {
      const high = Math.floor(n / value);
      const rest = n % value;
      const head = `${intToWords(high)} ${word}`;
      return rest ? `${head} ${intToWords(rest)}` : head;
    }
  }
  return String(n);
}

/** A number token (already stripped of grouping commas) to words. Integers read as a
 * whole; a fractional part reads digit-by-digit after "point" (how a listener expects a
 * version or measurement, e.g. "three point one four"). Beyond a trillion, read digits. */
function numberToWords(raw) {
  const neg = raw.startsWith("-");
  const body = neg ? raw.slice(1) : raw;
  const [intPart, fracPart] = body.split(".");
  const intN = Number(intPart);
  let words;
  if (!Number.isFinite(intN) || intN >= 1e12) {
    words = intPart
      .split("")
      .map((d) => ONES[Number(d)] ?? d)
      .join(" ");
  } else {
    words = intToWords(intN);
  }
  if (fracPart?.length) {
    words += ` point ${fracPart
      .split("")
      .map((d) => ONES[Number(d)] ?? d)
      .join(" ")}`;
  }
  return neg ? `minus ${words}` : words;
}

// --- emoji + symbols -------------------------------------------------------------------

// The few emoji worth speaking; everything else is dropped (a stray "grinning face"
// mid-sentence is worse than silence).
const EMOJI_WORDS = {
  "✅": " check ",
  "✔️": " check ",
  "☑️": " check ",
  "❌": " cross ",
  "✖️": " cross ",
  "⚠️": " warning ",
  "❗": " warning ",
  "‼️": " warning ",
  "⭐": " star ",
  "🌟": " star ",
};
// Strip emoji pictographs (astral planes + the misc-symbol/dingbat/geometric BMP blocks +
// variation selectors and ZWJ). Deliberately does NOT cover the arrows/math blocks — those
// carry meaning (→, ×, ÷) and are mapped to words by SYMBOL_WORDS first.
const EMOJI_STRIP =
  /(?:[\u2600-\u27BF\u2B00-\u2BFF]|\uFE0F|\u200D|\uD83C[\uDC00-\uDFFF]|\uD83D[\uDC00-\uDFFF]|\uD83E[\uDD00-\uDFFF])+/g;

// Symbols read badly (or dropped) by the voice if left as glyphs → spoken words. Applied
// after markdown emphasis (* _) is already stripped and numbers/currency verbalized, but
// BEFORE emoji stripping so the arrow/math glyphs become words instead of being dropped.
const SYMBOL_WORDS = [
  [/\s*→\s*/g, " to "],
  [/\s*⇒\s*/g, " to "],
  [/\s*←\s*/g, " from "],
  [/\s*&\s*/g, " and "],
  [/\s*\/\s*/g, " slash "],
  [/\s*@\s*/g, " at "],
  [/\s*=\s*/g, " equals "],
  [/\s*\+\s*/g, " plus "],
  [/\s*×\s*/g, " times "],
  [/\s*÷\s*/g, " divided by "],
  [/°/g, " degrees "],
  [/#/g, " number "],
  [/%/g, " percent "],
];

// --- URLs → spoken domain --------------------------------------------------------------

// A bare URL: read the registrable domain ("github dot com"), drop scheme/path/query —
// nobody wants a slug spelled out. www. is dropped.
const URL_RE = /\bhttps?:\/\/(?:www\.)?([^/\s)]+)[^\s)]*/gi;
const WWW_RE = /\bwww\.([^/\s)]+)[^\s)]*/gi;
const domainWords = (host) => host.replace(/\.+$/, "").split(".").join(" dot ");

// --- tables → sentences ----------------------------------------------------------------

const isTableSep = (line) => /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes("-");
const splitRow = (line) =>
  line
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((c) => c.trim());

/** Linearize GitHub-style pipe tables into one sentence per body row, pairing each header
 * with its cell ("Row one: Name, Alice. Age, thirty."). A listener can't see columns, so
 * the header labels carry the meaning. Non-table lines pass through untouched. */
function linearizeTables(text) {
  const lines = text.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const next = lines[i + 1];
    if (line.includes("|") && next != null && isTableSep(next)) {
      const headers = splitRow(line);
      out.push(`${headers.join(", ")}.`); // header row read as a lead-in
      i += 1; // skip the separator
      let r = 0;
      while (i + 1 < lines.length && lines[i + 1].includes("|") && !isTableSep(lines[i + 1])) {
        const cells = splitRow(lines[i + 1]);
        r += 1;
        const pairs = headers.map((h, c) => (h ? `${h}, ${cells[c] ?? ""}` : (cells[c] ?? "")));
        out.push(`Row ${intToWords(r)}: ${pairs.join(". ")}.`);
        i += 1;
      }
    } else {
      out.push(line);
    }
  }
  return out.join("\n");
}

// --- the pipeline ----------------------------------------------------------------------

const CITE_MODEL = /[ \t]?(?:【[^】\n]*†[^】\n]*】|\[[^\]\n]*†[^\]\n]*\])/g;
const CITE_SOURCE = /[ \t]?【\s*source\b[^】\n]*】/gi;
const CITE_CHIP = /\[\^\d+\]|【\^?\d+】/g;
const ENDS_SENTENCE = /[.!?:;]["')\]]?$/;

/**
 * Normalize answer Markdown to a single line of speakable prose.
 * @param {string} md
 * @returns {string}
 */
export function speakable(md) {
  let s = String(md || "");
  // Citations / browse-cursor chips — never read them.
  s = s.replace(CITE_MODEL, "").replace(CITE_SOURCE, "").replace(CITE_CHIP, "");
  // Code: fenced blocks are unintelligible spoken — announce, don't read. Inline code
  // keeps its token.
  s = s.replace(/```[\s\S]*?```/g, "\ncode block.\n").replace(/~~~[\s\S]*?~~~/g, "\ncode block.\n");
  s = s.replace(/`([^`]+)`/g, "$1");
  // Images drop; links keep their anchor text.
  s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, " ");
  s = s.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  // Tables → sentences (needs the multi-line block, so before per-line work).
  s = linearizeTables(s);
  // Per-line structure: drop heading/quote/list markers and horizontal rules, so only
  // prose remains. Marker removal happens before pause-authoring appends terminal marks.
  s = s
    .split("\n")
    .map((line) => {
      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) return ""; // horizontal rule
      return line
        .replace(/^\s{0,3}#{1,6}\s+/, "") // heading
        .replace(/^\s*>\s?/, "") // blockquote
        .replace(/^\s*[-*+]\s+/, "") // bullet
        .replace(/^\s*\d+\.\s+/, ""); // numbered
    })
    .join("\n");
  // Emphasis markers.
  s = s.replace(/(\*\*|__|\*|_|~~)/g, "");
  // PAUSE AUTHORING (before any whitespace collapse): every non-empty line that doesn't
  // already end in terminal punctuation gets a period, so each list item / heading /
  // paragraph becomes its own spoken sentence with a real pause.
  s = s
    .split("\n")
    .map((line) => line.trim())
    .map((line) => (line && !ENDS_SENTENCE.test(line) ? `${line}.` : line))
    .join("\n");
  // Token normalization.
  s = s.replace(URL_RE, (_m, host) => domainWords(host));
  s = s.replace(WWW_RE, (_m, host) => domainWords(host));
  // Currency: $1,250.50 → one thousand two hundred fifty dollars (and fifty cents).
  s = s.replace(/([$€£¥])\s?(\d[\d,]*)(?:\.(\d{2}))?/g, (_m, sym, whole, cents) => {
    const unit = { $: "dollars", "€": "euros", "£": "pounds", "¥": "yen" }[sym];
    const main = numberToWords(whole.replace(/,/g, ""));
    if (cents && cents !== "00") return `${main} ${unit} and ${numberToWords(cents)} cents`;
    return `${main} ${unit}`;
  });
  // Percent: 50% → fifty percent (before the generic % symbol map).
  s = s.replace(
    /(\d[\d,]*(?:\.\d+)?)\s?%/g,
    (_m, n) => `${numberToWords(n.replace(/,/g, ""))} percent`,
  );
  // Numeric ranges: 3-5 → three to five.
  s = s.replace(
    /\b(\d[\d,]*(?:\.\d+)?)\s?[-–]\s?(\d[\d,]*(?:\.\d+)?)\b/g,
    (_m, a, b) => `${numberToWords(a.replace(/,/g, ""))} to ${numberToWords(b.replace(/,/g, ""))}`,
  );
  // Remaining standalone numbers (with grouping commas), left as digits inside words.
  s = s.replace(/\b\d[\d,]*(?:\.\d+)?\b/g, (m) => numberToWords(m.replace(/,/g, "")));
  // Symbols → words (before emoji stripping, so arrows/math survive as words).
  for (const [re, word] of SYMBOL_WORDS) s = s.replace(re, word);
  // Emoji: verbalize the allow-list, drop the rest.
  for (const [glyph, word] of Object.entries(EMOJI_WORDS)) s = s.split(glyph).join(word);
  s = s.replace(EMOJI_STRIP, " ");
  // Tidy: no space before punctuation (a dropped emoji/symbol can leave one), collapse.
  return s
    .replace(/\s+([.!?,;:])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}
