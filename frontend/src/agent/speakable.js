// Turn an assistant answer's Markdown into legible, speakable plain text for TTS.
//
// The read-aloud text normalizer for the PWA (imported here, typed via speakable.d.ts).
// Authored as plain ESM — no framework, no deps — so it can ALSO be loaded verbatim by the
// wall display's index.html (which has no build step); that adoption, and a byte-parity
// guard between the two copies, land with the wall restructure in Wave 0 of
// docs/plans/READ_ALOUD_LEGIBILITY.md. Until then the wall still uses its own mdToPlain.
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

// Denominator words for a spoken fraction; only small, unambiguous denominators.
const FRACTION_DENOM = {
  2: "half",
  3: "third",
  4: "quarter",
  5: "fifth",
  6: "sixth",
  7: "seventh",
  8: "eighth",
  9: "ninth",
  10: "tenth",
};

/** A PROPER fraction "n/d" (small known denominator, n < d) to words: "3/4" → "three quarters".
 * null for anything else so the caller leaves "/" to the slash map — this is what keeps dates
 * and ratios ("07/04", "24/7", "16/9") from being mis-said as fractions. */
function fractionWords(numStr, denStr) {
  const num = Number(numStr);
  const den = Number(denStr);
  const denom = FRACTION_DENOM[den];
  if (!denom || num < 1 || num >= den) return null;
  const plural = den === 2 ? "halves" : `${denom}s`;
  return `${numberToWords(String(num))} ${num === 1 ? denom : plural}`;
}

// --- dates, temperatures, distances → words (before the generic number pass) -----------

// These depend on the RAW digits, so they run before numbers are verbalized below (which would
// otherwise read a date's day as a cardinal "ten" and its year as "two thousand twenty six", and
// leave "°F"/"mi" stranded next to a spelled-out number). Mirrors the box-side backstop in
// deploy/tts-stt/piper_server.py for the wall path, which never verbalizes numbers.
const MONTHS =
  "January|February|March|April|May|June|July|August|September|October|November|December";
const DATE_RE = new RegExp(
  `\\b(${MONTHS})\\s+(\\d{1,2})(?:st|nd|rd|th)?(?:,?\\s+(\\d{4}))?\\b`,
  "g",
);
const ORD_ONES = {
  1: "first",
  2: "second",
  3: "third",
  4: "fourth",
  5: "fifth",
  6: "sixth",
  7: "seventh",
  8: "eighth",
  9: "ninth",
  10: "tenth",
  11: "eleventh",
  12: "twelfth",
  13: "thirteenth",
  14: "fourteenth",
  15: "fifteenth",
  16: "sixteenth",
  17: "seventeenth",
  18: "eighteenth",
  19: "nineteenth",
  20: "twentieth",
  30: "thirtieth",
};
const DEGREE_UNITS = { F: "Fahrenheit", C: "Celsius", K: "Kelvin" };

/** A day 1-31 as an ordinal ("10" → "tenth", "21" → "twenty first"). */
function ordinalDay(n) {
  return ORD_ONES[n] ?? `${TENS[Math.floor(n / 10)]} ${ORD_ONES[n % 10]}`;
}

/** A 4-digit year in speech style: 2026 → "twenty twenty six", 1999 → "nineteen ninety nine",
 * 2000 → "two thousand", 2005 → "two thousand five", 1905 → "nineteen oh five". */
function yearWords(y) {
  const hi = Math.floor(y / 100);
  const lo = y % 100;
  if (lo === 0) {
    return y % 1000 === 0 ? `${intToWords(y / 1000)} thousand` : `${intToWords(hi)} hundred`;
  }
  if (lo < 10) {
    return y >= 2000 && y < 2010 ? `two thousand ${ONES[lo]}` : `${intToWords(hi)} oh ${ONES[lo]}`;
  }
  return `${intToWords(hi)} ${intToWords(lo)}`;
}

/** Verbalize dates, temperature units, and the "mi" distance unit while their digits are intact.
 * "July 10, 2026" → "July tenth, twenty twenty six"; "94°F" → "94 degrees Fahrenheit" (the "94"
 * is spelled by the later number pass); "40 mi" → "40 miles". */
function measuresToWords(s) {
  return s
    .replace(DATE_RE, (m, month, dayStr, yearStr) => {
      const day = Number(dayStr);
      if (day < 1 || day > 31) return m; // not a day-of-month — leave untouched
      const said = `${month} ${ordinalDay(day)}`;
      return yearStr ? `${said}, ${yearWords(Number(yearStr))}` : said;
    })
    .replace(/°\s*([FCK])\b/g, (_m, u) => ` degrees ${DEGREE_UNITS[u]}`)
    .replace(/\b(\d[\d,]*(?:\.\d+)?)\s*mi\b/g, "$1 miles");
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
  [/\|/g, " "], // a stray pipe (non-table) reads as nothing, never "bar"
];

// Abbreviations espeak (piper's phonemizer) reads wrong or with a sentence-splitting pause.
// e.g./i.e.: it spells "i.e." as "aye ee"; we expand and consume any trailing comma so the
// aside carries exactly one pause whether the source wrote "e.g., X" or "e.g. X". Titles: espeak
// expands "Mr."→"Mister" but treats the "." as a SENTENCE END, so "Mr. Lee" gets an unwanted
// pause ("Mister" ⏸ "Lee"); dropping the period here keeps the clause together. vs/approx: espeak
// reads "vs"→"V S" and "approx"→"approx", so we say the words. Ordinals (1st→first) and repeated
// "!!!"/"..." need no rule — espeak already handles them; verified against espeak-ng 1.51.
const ABBREVIATIONS = [
  [/\be\.g\.\s*,?/gi, "for example, "],
  [/\bi\.e\.\s*,?/gi, "that is, "],
  [/\bMr\.\s*/g, "Mister "],
  [/\bMrs\.\s*/g, "Missus "],
  [/\bMs\.\s*/g, "Miz "],
  [/\bDr\.\s*/g, "Doctor "],
  [/\bProf\.\s*/g, "Professor "],
  [/\bvs\.?\s*/gi, "versus "],
  [/\bapprox\.\s*/gi, "approximately "],
];

// Per-engine utterance profiles — the pronunciation rules that depend on the voice's PHONEMIZER.
// piper embeds espeak-ng and our Kokoro build phonemizes through espeak-ng too, so both share one
// ruleset today; keeping the seam here means giving Kokoro its own (misaki-aware) profile later is
// a config change, not a re-thread of the pipeline. Engine-agnostic work lives in toProse().
const UTTERANCE_PROFILES = {
  piper: { abbreviations: ABBREVIATIONS },
  kokoro: { abbreviations: ABBREVIATIONS },
};

// --- URLs → spoken domain --------------------------------------------------------------

// A bare URL: read the registrable domain ("github dot com"), drop scheme/path/query —
// nobody wants a slug spelled out. www. is dropped.
const URL_RE = /\bhttps?:\/\/(?:www\.)?([^/\s)]+)[^\s)]*/gi;
const WWW_RE = /\bwww\.([^/\s)]+)[^\s)]*/gi;
const domainWords = (host) => host.replace(/\.+$/, "").split(".").join(" dot ");

// --- tables → sentences ----------------------------------------------------------------

// A table delimiter row: dashes/colons/pipes only, AND at least one pipe — so a bare
// horizontal rule ("-----") under a prose line that happens to contain a "|" is NOT
// mistaken for a table header.
const isTableSep = (line) =>
  /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes("-") && line.includes("|");
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
        // Iterate over the WIDER of header/row so a ragged row with extra cells isn't
        // silently dropped (the extra cells read without a header label).
        const width = Math.max(headers.length, cells.length);
        const pairs = [];
        for (let c = 0; c < width; c++) {
          const h = headers[c];
          pairs.push(h ? `${h}, ${cells[c] ?? ""}` : (cells[c] ?? ""));
        }
        // Pairs joined with "; " (not ". ") so the whole row stays ONE spoken clip — a bare
        // "Age, thirty." clip would lose the "Row one" context.
        out.push(`Row ${intToWords(r)}: ${pairs.join("; ")}.`);
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
const ENDS_SENTENCE = /[.!?:;…]["')\]]?$/;

/**
 * Structural, ENGINE-AGNOSTIC pass: Markdown → plain multi-line prose. Strips citations and
 * markup, linearizes tables, drops heading/quote/bullet markers (spelling a numbered marker),
 * removes emphasis. Newlines are PRESERVED — the utterance pass authors pauses from them. Every
 * TTS engine needs this identically.
 * @param {string} md
 * @returns {string}
 */
export function toProse(md) {
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
  // Per-line structure: drop heading/quote/bullet markers and horizontal rules, so only prose
  // remains. A NUMBERED item keeps its number as a spoken word ("4." → "four.") so the listener
  // hears the enumeration — a bare bullet carries no such info, so it's dropped. Marker handling
  // happens before pause-authoring appends terminal marks.
  s = s
    .split("\n")
    .map((line) => {
      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) return ""; // horizontal rule
      return line
        .replace(/^\s{0,3}#{1,6}\s+/, "") // heading
        .replace(/^\s*>\s?/, "") // blockquote
        .replace(/^\s*[-*+]\s+/, "") // bullet
        .replace(/^(\s*)(\d+)\.\s+/, (_m, indent, n) => `${indent}${numberToWords(n)}. `); // numbered → spoken
    })
    .join("\n");
  // Emphasis markers.
  s = s.replace(/(\*\*|__|\*|_|~~)/g, "");
  return s;
}

/**
 * Pronunciation + pacing pass over `prose` from toProse, tuned to `engine`'s phonemizer. Verbalizes
 * abbreviations/numbers/currency/percent/fractions/symbols/emoji/URLs, authors pauses from line
 * ends, and shapes dashes/parentheticals into comma beats. Returns ONE speakable line.
 * @param {string} prose
 * @param {"piper" | "kokoro"} [engine]
 * @returns {string}
 */
export function toUtterance(prose, engine = "piper") {
  const profile = UTTERANCE_PROFILES[engine] ?? UTTERANCE_PROFILES.piper;
  let s = String(prose || "");
  // Latin abbreviations → words (before pause-authoring, so their interior dots aren't read
  // as sentence ends and the spoken aside carries a real pause).
  for (const [re, word] of profile.abbreviations) s = s.replace(re, word);
  // A single-letter name initial ("Dennis E. Taylor", "J. R. R. Tolkien") — drop the period, like
  // titles above. Its "." otherwise reads as a SENTENCE END: espeak takes a long pause AND the clip
  // splitter cuts the name in two (a separate render = an audible gap), which compound. Gated to an
  // initial FOLLOWED by a capitalized word (a surname / next initial) and NOT part of a dotted
  // abbreviation like "U.S.", so a real single-letter sentence end is rarely touched.
  s = s.replace(/(?<!\.)\b([A-Z])\.(?=\s+[A-Z])/g, "$1");
  // Ellipsis: normalize "..."/"…" to a single ellipsis char. espeak renders it as a ~300 ms
  // trailing beat — longer than a comma, no spoken "dot dot dot" — and the chunker never cuts on
  // it (it's not . ! ?), so the dramatic pause stays inside the clause instead of splitting it.
  // Only horizontal whitespace is eaten, NOT a following newline — a line-ENDING ellipsis is a
  // paragraph break the pause-authoring below must still see (else it runs into the next line).
  s = s.replace(/[ \t]*(?:\.{2,}|…)[ \t]*/g, "… ");
  // PAUSE AUTHORING (before any whitespace collapse): every non-empty line that doesn't
  // already end in terminal punctuation gets a period, so each list item / heading /
  // paragraph becomes its own spoken sentence with a real pause.
  const lines = s.split("\n").map((line) => line.trim());
  s = lines
    .map((line, i) => {
      if (!line) return line;
      // A line ENDING in "…" with more content after it is a trailing-off at a line/paragraph
      // break: make it a hard stop so it becomes its own clip (splitClips cuts on "." not "…", so
      // otherwise it runs straight into the next line). A last-line "…" stays a soft dramatic beat.
      if (line.endsWith("…")) return lines.slice(i + 1).some(Boolean) ? `${line}.` : line;
      return ENDS_SENTENCE.test(line) ? line : `${line}.`;
    })
    .join("\n");
  // Dates / temperatures / distances — BEFORE any number verbalization, which needs the raw digits.
  s = measuresToWords(s);
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
  // Simple proper fractions BEFORE the slash map turns "/" into "slash": 3/4 → three quarters.
  // Gated to n < d with a known small denominator and no adjacent digit/slash/dot, so dates and
  // ratios ("07/04", "24/7", "16/9") fall through to the slash map untouched.
  s = s.replace(
    /(?<![\d/.])(\d{1,2})\/(\d{1,2})(?![\d/.])/g,
    (m, a, b) => fractionWords(a, b) ?? m,
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
  // Dashes vs compound hyphens. An em/en/bar dash, or a spaced hyphen, is a clause break → a
  // comma beat ("yours—let's see" → "yours, let's see", "guess it — great" → "guess it, great").
  // A hyphen BETWEEN two word characters is a compound ("large‑scale", "well-known", "Bob‑verse"):
  // espeak MASHES an ASCII compound into one word ("largescale"), so split it to a space for two
  // clean words — how a person reads it, no pause. Covers the ASCII, Unicode and non-breaking
  // hyphens (U+2010/U+2011). Numeric ranges (3–5 → "three to five") were handled above.
  s = s
    .replace(/\s*[–—―]\s*/g, ", ")
    .replace(/\s+-\s+/g, ", ")
    .replace(/(?<=[^\W_])[-‐‑](?=[^\W_])/g, " ");
  // Parentheticals: piper carries no pause across ( ), so it races the aside into the
  // surrounding clause in one breath. Bracket it with commas instead — a beat on each side —
  // so "spending (target 5%) and reaffirm" reads as "spending, target five percent, and
  // reaffirm". (Markdown links/images + fenced code already had their parens removed above,
  // so only prose asides reach here.)
  s = s.replace(/\s*\(\s*/g, ", ").replace(/\s*\)/g, ",");
  // Tidy: no space before punctuation (a dropped emoji/symbol can leave one); fold a comma
  // that a bracket left touching stronger punctuation ("2035)." → "2035,." → "2035.", and
  // "(He agreed.)" → ".," → "."), then any doubled/leading comma; finally collapse whitespace.
  return s
    .replace(/\s+([.!?,;:])/g, "$1")
    .replace(/,+(?=[.!?;:])/g, "")
    .replace(/([.!?;:]),+/g, "$1")
    .replace(/,{2,}/g, ",")
    .replace(/^\s*,\s*/, "")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Normalize answer Markdown to a single line of speakable prose for `engine` (default piper) —
 * the engine-agnostic structural pass composed with the engine-specific utterance pass.
 * @param {string} md
 * @param {"piper" | "kokoro"} [engine]
 * @returns {string}
 */
export function speakable(md, engine = "piper") {
  return toUtterance(toProse(md), engine);
}

/**
 * The utterance profile a box voice id renders through: a Kokoro voice ("kokoro-…") phonemizes
 * via Kokoro, every other id via piper/espeak. Lets a caller thread the right engine into
 * speakable/chunkStream from the chosen voice alone — so custom text and a chat answer are
 * normalized for the SAME engine that will actually render them.
 * @param {string} voice
 * @returns {"piper" | "kokoro"}
 */
export function engineForVoice(voice) {
  return String(voice || "").startsWith("kokoro-") ? "kokoro" : "piper";
}

// --- streaming chunker -----------------------------------------------------------------

const ENDS_LINE = /[.!?]["')\]]?\s*$/;
// A line that begins a new block (so the previous line is a safe cut point even without
// terminal punctuation): heading, list item, blockquote, table row, fence, or blank.
const BLOCK_START = /^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s|>|\||`{3}|~{3})|^\s*$/;

/** Split NORMALIZED text (one line, no newlines; decimals already verbalized) into
 * sentence-sized clips for piper — on whitespace that follows terminal punctuation.
 * Lossless (a lookbehind split keeps every character, even across abbreviations). */
function splitClips(norm) {
  return norm
    .split(/(?<=[.!?])\s+/)
    .map((c) => c.trim())
    .filter(Boolean);
}

// Abbreviations whose trailing "." never ends a sentence (titles before a name, e.g./i.e.) —
// so the streaming committer doesn't cut a clip mid-name ("Dr. Smith" → "Doctor" ⏸ "Smith"). A
// lone letter is a name initial ("Dennis E. Taylor"): held for the same reason (the utterance pass
// drops its period, so the real sentence end follows). Kept to never-sentence-final tokens only.
const ABBREV_NO_BREAK = /(?:^|[\s("'‘“])(?:mr|mrs|ms|dr|prof|vs|approx|e\.g|i\.e|[a-z])\.$/i;

/** In the trailing (newline-less) line, the length up to the last COMPLETE sentence — so a
 * partial final sentence is held for the next delta. A terminator is only a boundary when
 * WHITESPACE follows it: a buffer that ends right at a "." is ambiguous mid-stream (it could
 * be "example." → "example.com"), so it waits for the next char (flush commits the tail). A
 * "." inside an ellipsis run or right after a known abbreviation is NOT a boundary, so a clip
 * isn't cut where piper shouldn't pause — the real sentence end (which speakable normalizes
 * the abbreviation/ellipsis away before) follows. */
function intraLineSafe(line) {
  let last = 0;
  const re = /[.!?]["')\]]?\s/g;
  for (let m = re.exec(line); m !== null; m = re.exec(line)) {
    const t = m.index;
    if (line[t] === "." && (line[t - 1] === "." || ABBREV_NO_BREAK.test(line.slice(0, t + 1)))) {
      continue; // ellipsis run or abbreviation — not a real sentence boundary
    }
    last = re.lastIndex;
  }
  return last;
}

/** The largest prefix of `raw` made only of COMPLETE units — never inside an open code
 * fence or a still-streaming table, ending at a sentence/line boundary. */
function committedLen(raw) {
  const lines = raw.split("\n");
  const starts = [];
  let off = 0;
  for (const ln of lines) {
    starts.push(off);
    off += ln.length + 1;
  }
  const lastIdx = lines.length - 1;
  let inFence = false;
  let fenceOpenAt = -1;
  let tableStart = -1; // offset where the current trailing table run began (-1 = none)
  let safe = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const isLast = i === lastIdx;
    const lineEnd = starts[i] + line.length + (isLast ? 0 : 1);
    if (/^\s*(?:```|~~~)/.test(line)) {
      if (inFence) {
        inFence = false;
        fenceOpenAt = -1;
        if (!isLast) safe = lineEnd; // closing fence -> committable
      } else {
        inFence = true;
        fenceOpenAt = starts[i];
      }
      continue;
    }
    if (inFence) continue; // inside code -> not committable
    if (isLast) {
      const s = intraLineSafe(line); // trailing partial line -> commit whole sentences only
      if (s > 0 && tableStart < 0) safe = starts[i] + s;
      continue;
    }
    const next = lines[i + 1];
    if (line.includes("|")) {
      if (tableStart < 0) tableStart = starts[i];
      // A table is complete only when a genuine terminating line follows — NOT another row,
      // and NOT the empty trailing artifact of a buffer that just ends in a newline (a next
      // row could still stream in). Hold the whole run until then.
      const nextIsRow = next.includes("|");
      const nextIsTrailingBlank = i + 1 === lastIdx && next === "";
      if (nextIsRow || nextIsTrailingBlank) continue;
      safe = lineEnd; // table terminated by a real non-table line
      tableStart = -1;
      continue;
    }
    // A newline-terminated prose line commits if the next line starts a new block/blank or
    // this line ends a sentence — otherwise it's a soft-wrapped continuation, so hold.
    if (BLOCK_START.test(next) || ENDS_LINE.test(line)) safe = lineEnd;
  }
  // Never commit into an open code fence or a still-streaming table at the buffer tail.
  if (inFence && fenceOpenAt >= 0 && safe > fenceOpenAt) safe = fenceOpenAt;
  if (tableStart >= 0 && safe > tableStart) safe = tableStart;
  return safe;
}

/**
 * Streaming, block-aware splitter for read-aloud. Given the raw markdown received since the
 * caller's cursor, return normalized speakable clips for the COMPLETE units it can emit now,
 * plus how many RAW chars were consumed (advance a raw-space cursor — stable across deltas).
 * Incomplete trailing blocks (an open ``` fence, a table still streaming) and a partial
 * trailing sentence are held until more arrives (or `flush`). Blocks normalize whole.
 * @param {string} raw
 * @param {boolean} flush
 * @param {"piper" | "kokoro"} [engine]
 * @returns {{ chunks: string[]; consumed: number }}
 */
export function chunkStream(raw, flush, engine = "piper") {
  const committed = flush ? raw.length : committedLen(raw);
  if (committed <= 0) return { chunks: [], consumed: 0 };
  return { chunks: splitClips(speakable(raw.slice(0, committed), engine)), consumed: committed };
}

// --- reading profile (markup vs prose) -------------------------------------------------

/**
 * Classify an answer's Markdown as heavy "markup" (a structured LLM answer — headings, lists,
 * tables, code, dense inline emphasis) vs "prose" (a story / plain paragraphs). Drives AUTOMATIC
 * audiobook pacing without a mode switch: a prose turn gets a slower, spaced read; a markup turn
 * stays snappy. A heuristic threshold — tune by feel. Blank text reads as prose (a bare line is
 * fine either way; prose pacing is the gentle one).
 * @param {string} md
 * @returns {"markup" | "prose"}
 */
export function readingProfile(md) {
  const text = String(md || "");
  const lines = text.split("\n").filter((l) => l.trim());
  if (!lines.length) return "prose";
  let structural = 0;
  let fenced = false;
  for (const line of lines) {
    if (/^\s*(?:```|~~~)/.test(line)) {
      fenced = true; // a code fence — unmistakably an answer, not a story
      structural += 1;
    } else if (
      /^\s{0,3}#{1,6}\s/.test(line) || // heading
      /^\s*[-*+]\s/.test(line) || // bullet
      /^\s*\d+\.\s/.test(line) || // numbered item
      /^\s*>/.test(line) || // blockquote
      line.includes("|") // table-ish row
    ) {
      structural += 1;
    }
  }
  // Inline emphasis / code / links — a few is prose flavour, many is a structured answer.
  const inlineMarks = (text.match(/\*\*|__|`|\]\(/g) || []).length;
  return structural / lines.length >= 0.25 || fenced || inlineMarks >= 4 ? "markup" : "prose";
}
