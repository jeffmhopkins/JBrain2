// Turn an assistant answer's Markdown into legible, speakable plain text for TTS.
//
// The read-aloud text normalizer for the PWA (imported here, typed via speakable.d.ts).
// Authored as plain ESM — no framework, no deps — so it can ALSO be loaded verbatim by the
// wall display's index.html (which has no build step); that adoption, and a byte-parity
// guard between the two copies, land with the wall restructure in Wave 0 of
// docs/archive/READ_ALOUD_LEGIBILITY.md. Until then the wall still uses its own mdToPlain.
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

/** Spell a run of digits one-by-one ("4567" → "four five six seven") — for phone numbers and
 * other id-like sequences a listener wants heard as digits, not a magnitude. */
function digitsToWords(digits) {
  return digits
    .split("")
    .map((d) => ONES[Number(d)] ?? d)
    .join(" ");
}

/** A clock time to words. am/pm (already normalized to "A M"/"P M" upstream, or a bare "am"/"pm"
 * captured here) reads 12-hour; a bare 13–23 hour reads 24-hour ("fourteen hundred"). */
function timeWords(hStr, mStr, mer) {
  const h = Number(hStr);
  const m = Number(mStr);
  const min = m === 0 ? "" : m < 10 ? `oh ${ONES[m]}` : intToWords(m);
  const meridiem = mer ? ` ${mer.replace(/[.\s]/g, "").toUpperCase().split("").join(" ")}` : "";
  if (meridiem || (h >= 1 && h <= 12)) {
    const hour = intToWords(h === 0 ? 12 : h);
    return m === 0 ? `${hour} o'clock${meridiem}` : `${hour} ${min}${meridiem}`;
  }
  return m === 0 ? `${intToWords(h)} hundred` : `${intToWords(h)} ${min}`;
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
const MONTH_NAMES = MONTHS.split("|");
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
  return (
    s
      // ISO dates (2026-08-10) → "August tenth, twenty twenty six", BEFORE the numeric-range rule
      // reads the hyphens as "to". A phone number is 3-3-4 digits, never 4-2-2, so it can't match.
      .replace(/\b(\d{4})-(\d{2})-(\d{2})\b/g, (m, y, mo, d) => {
        const month = Number(mo);
        const day = Number(d);
        if (month < 1 || month > 12 || day < 1 || day > 31) return m;
        return `${MONTH_NAMES[month - 1]} ${ordinalDay(day)}, ${yearWords(Number(y))}`;
      })
      .replace(DATE_RE, (m, month, dayStr, yearStr) => {
        const day = Number(dayStr);
        if (day < 1 || day > 31) return m; // not a day-of-month — leave untouched
        const said = `${month} ${ordinalDay(day)}`;
        return yearStr ? `${said}, ${yearWords(Number(yearStr))}` : said;
      })
      .replace(/°\s*([FCK])\b/g, (_m, u) => ` degrees ${DEGREE_UNITS[u]}`)
      .replace(/\b(\d[\d,]*(?:\.\d+)?)\s*mi\b/g, "$1 miles")
  );
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
  // Inequalities/relations — dropped glyphs silently INVERT a sentence's meaning, so speak them.
  // The composite forms (<=, >=, !=, ≤, ≥, ≠) come before the "=" and "<"/">" rules below so they
  // aren't half-consumed. Numbers around them are already words by the time SYMBOL_WORDS runs.
  [/\s*(?:<=|≤)\s*/g, " less than or equal to "],
  [/\s*(?:>=|≥)\s*/g, " greater than or equal to "],
  [/\s*(?:!=|≠)\s*/g, " not equal to "],
  [/\s*±\s*/g, " plus or minus "],
  [/\s*<\s*/g, " less than "],
  [/\s*>\s*/g, " greater than "],
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
  // Lowercase / mixed-case dotted forms the uppercase U.S. collapse (in toUtterance) can't catch —
  // spell them as dot-free spaced letters so their interior periods never read as a sentence end.
  [/\ba\.m\.\s*/gi, "A M "],
  [/\bp\.m\.\s*/gi, "P M "],
  [/\bPh\.?\s*D\.?\s*/g, "P H D "],
];

// Utterance profile — the pronunciation rules that depend on the voice's PHONEMIZER. Kokoro is the
// only on-box engine (piper was removed); the seam is kept so a future misaki-specific ruleset is a
// config change, not a re-thread of the pipeline. Engine-agnostic work lives in toProse().
const UTTERANCE_PROFILES = {
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
      // A heading read aloud sounds flat as an isolated 1-word clip ending in "." — end it with
      // ":" instead (a pause cue splitClips does NOT cut on, since it splits only on . ! ?), so the
      // heading flows into the FOLLOWING sentence with lead-in intonation instead of a lone
      // low-affect monotone fragment. A heading already ending in terminal punctuation is left.
      const heading = line.match(/^\s{0,3}#{1,6}\s+(.*\S)\s*$/);
      if (heading) return /[.!?:;…]$/.test(heading[1]) ? heading[1] : `${heading[1]}:`;
      return line
        .replace(/^\s*>\s?/, "") // blockquote
        .replace(/^\s*[-*+]\s+/, "") // bullet
        .replace(/^(\s*)(\d+)\.\s+/, (_m, indent, n) => `${indent}${numberToWords(n)}. `); // numbered → spoken
    })
    .join("\n");
  // snake_case: an underscore BETWEEN two alphanumerics is a compound separator ("user_id"), not
  // emphasis — turn it into a space (two clean words) BEFORE the emphasis stripper below would fold
  // "user_id" into "userid". Gated to alnum_alnum (not `_`), so a `__dunder__` / `_italic_` boundary
  // underscore is left for the emphasis pass.
  s = s.replace(/(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])/g, " ");
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
export function toUtterance(prose, engine = "kokoro") {
  const profile = UTTERANCE_PROFILES[engine] ?? UTTERANCE_PROFILES.kokoro;
  let s = String(prose || "");
  // Latin abbreviations → words (before pause-authoring, so their interior dots aren't read
  // as sentence ends and the spoken aside carries a real pause).
  for (const [re, word] of profile.abbreviations) s = s.replace(re, word);
  // Dotted initialisms (U.S., U.K., U.N., E.U., D.C., U.S.A.): strip the interior + trailing
  // periods so the phonemizer reads the LETTERS ("U S") instead of taking a sentence-ending pause
  // on each dot — and so splitClips / intraLineSafe (below + in the chunker) don't cut "The U.S.
  // economy" into two separate renders (the audible gap this fixes). Uppercase-only, >=2 letter
  // groups, so a lowercase domain and a lone name initial ("Dennis E.") are untouched; the
  // lowercase/mixed a.m./p.m./Ph.D. cases are handled in ABBREVIATIONS above. When such an
  // initialism truly ends a sentence, pause-authoring below re-adds the "." so the stop survives.
  s = s.replace(/\b(?:[A-Z]\.){2,}/g, (m) => m.replace(/\./g, " ").trim());
  // A single-letter name initial ("Dennis E. Taylor", "J. R. R. Tolkien") — drop the period, like
  // titles above. Its "." otherwise reads as a SENTENCE END: espeak takes a long pause AND the clip
  // splitter cuts the name in two (a separate render = an audible gap), which compound. Gated to an
  // initial FOLLOWED by a capitalized word (a surname / next initial); the dotted initialisms above
  // were already collapsed, so a real single-letter sentence end is rarely touched.
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
  // Phone numbers (NNN-NNN-NNNN, dot/space separated too, optional "(NNN)") → digit-by-digit with a
  // beat between groups. BEFORE the numeric-range rule, which would otherwise read "555-123" as
  // "five hundred fifty five to one hundred twenty three". The 3-3-4 shape can't match an ISO date
  // (4-2-2) or a version (1-1-1).
  s = s.replace(
    /\(?(\d{3})\)?[-.\s](\d{3})[-.\s](\d{4})\b/g,
    (_m, a, b, c) => `${digitsToWords(a)}, ${digitsToWords(b)}, ${digitsToWords(c)}`,
  );
  // Version numbers (v2.3.1 — three+ dot-separated groups) → "version two point three point one".
  // Gated to 2+ dots so an ordinary decimal ("2.3") falls through to the number pass untouched.
  s = s.replace(
    /\b(v)?(\d+(?:\.\d+){2,})\b/gi,
    (_m, v, nums) => `${v ? "version " : ""}${nums.split(".").map(numberToWords).join(" point ")}`,
  );
  // Clock times (3:30, 3:30pm, 14:00) → words, BEFORE the generic number pass leaves a stray colon.
  // A trailing "pm"/"am" reads as spaced letters; an already-expanded "P M" (from the abbreviation
  // pass) simply follows the spelled time. A ratio like "16:9" has a 1-digit tail and never matches.
  s = s.replace(/\b(\d{1,2}):(\d{2})(?:\s*([ap]m)\b)?/gi, (_m, h, mm, mer) =>
    timeWords(h, mm, mer),
  );
  // Token normalization.
  s = s.replace(URL_RE, (_m, host) => domainWords(host));
  s = s.replace(WWW_RE, (_m, host) => domainWords(host));
  // Currency: $1,250.50 → "... dollars and fifty cents"; $16.8 billion → "sixteen point eight
  // billion dollars"; $5 million → "five million dollars"; $16.8 → "sixteen point eight dollars".
  // The unit trails the amount, so a magnitude word (million/billion/…) and a non-cents decimal read
  // in the right order — the old two-decimal-only regex dropped both (matching "$16" out of "$16.8
  // billion" and orphaning ".8 billion"). A bare two-decimal amount still reads as dollars-and-cents.
  s = s.replace(
    /([$€£¥])\s?(\d[\d,]*(?:\.\d+)?)(?:\s+(trillion|billion|million|thousand)\b)?/gi,
    (_m, sym, amount, magnitude) => {
      const unit = { $: "dollars", "€": "euros", "£": "pounds", "¥": "yen" }[sym];
      const clean = amount.replace(/,/g, "");
      const cents =
        !magnitude && /\.\d{2}$/.test(clean) ? clean.slice(clean.indexOf(".") + 1) : null;
      if (cents !== null) {
        const whole = numberToWords(clean.slice(0, clean.indexOf(".")));
        return cents === "00"
          ? `${whole} ${unit}`
          : `${whole} ${unit} and ${numberToWords(cents)} cents`;
      }
      const num = numberToWords(clean);
      return magnitude ? `${num} ${magnitude.toLowerCase()} ${unit}` : `${num} ${unit}`;
    },
  );
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
  // Dashes vs compound hyphens. An em/bar dash, or a SPACED en-dash/hyphen, is a clause break → a
  // comma beat ("yours—let's see" → "yours, let's see", "guess it — great" → "guess it, great").
  // But a CLOSED-UP dash between two word characters is a compound, NOT a pause: an ASCII/Unicode
  // hyphen ("large‑scale", "well-known") that espeak would otherwise MASH into one word, AND a
  // closed-up en-dash relation ("U.S.–Iran" → "U S Iran", "New York–London") that the old
  // all-en-dashes-to-comma rule turned into an awkward "U S, Iran" pause. Both split to a space for
  // two clean words. Covers the ASCII, Unicode and non-breaking hyphens (U+2010/U+2011) and the
  // en-dash (U+2013). Numeric ranges (3–5 → "three to five") were handled above; a leftover en-dash
  // (not a word–word compound, not spaced) falls through to a comma beat as before.
  s = s
    .replace(/\s*[—―]\s*/g, ", ")
    .replace(/\s+–\s*|\s*–\s+/g, ", ")
    .replace(/\s+-\s+/g, ", ")
    .replace(/(?<=[^\W_])[-‐‑–](?=[^\W_])/g, " ")
    .replace(/\s*–\s*/g, ", ");
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
export function speakable(md, engine = "kokoro") {
  return toUtterance(toProse(md), engine);
}

/**
 * Prepare a deep-research REPORT's Markdown for read-aloud: the report is a written artifact with
 * visual scaffolding a listener shouldn't hear. Two removals (the body stays untouched):
 *  - **section headings** — a spoken briefing shouldn't voice its `## Good Morning` labels; read
 *    aloud they become terse sentences that double the prose ("Good Morning." then "Good morning,
 *    it's Sunday…"). Drop the whole heading LINE (not just the `#`), leaving the prose to flow on
 *    its own verbal transitions.
 *  - **the trailing Sources/References/Citations section** — TTS would otherwise read the URL list
 *    aloud ("Sources. florida spacereport dot blogspot dot com…").
 * Returns Markdown (still fed through `speakable`/`chunkStream` by the caller), so only the report
 * read-aloud path opts in — chat read-aloud is byte-unchanged.
 * @param {string} md
 * @returns {string}
 */
export function reportToSpeech(md) {
  let s = String(md || "");
  // Cut a trailing Sources/References/Citations heading and everything after it (to end).
  s = s.replace(/\n\s*#{1,6}\s+(?:sources|references|citations)\b[\s\S]*$/i, "\n");
  // Drop heading LINES entirely (the marker is `#{1,6}` at line start).
  s = s
    .split("\n")
    .filter((line) => !/^\s{0,3}#{1,6}\s/.test(line))
    .join("\n");
  return s;
}

/**
 * The utterance profile a box voice renders through. Kokoro is the only on-box engine now, so this
 * always resolves to "kokoro" — the seam is kept so callers still thread an engine explicitly and a
 * future profile split is a one-line change here.
 * @param {string} _voice
 * @returns {"kokoro"}
 */
export function engineForVoice(_voice) {
  return "kokoro";
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
// drops its period, so the real sentence end follows). The `(?:[a-z]\.)*[a-z]` run also matches a
// dotted initialism ("U.S.", "a.m.", "D.C.") so the committer holds "The U.S. economy" whole instead
// of cutting at "U.S." (the utterance pass collapses it to "U S"). Never-sentence-final tokens only.
const ABBREV_NO_BREAK =
  /(?:^|[\s("'‘“])(?:mr|mrs|ms|dr|prof|vs|approx|e\.g|i\.e|(?:[a-z]\.)*[a-z])\.$/i;

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
  let headingHold = -1; // offset of a trailing heading not yet joined by committed content (-1 = none)
  let safe = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const isLast = i === lastIdx;
    const lineEnd = starts[i] + line.length + (isLast ? 0 : 1);
    if (/^\s*(?:```|~~~)/.test(line)) {
      if (inFence) {
        inFence = false;
        fenceOpenAt = -1;
        if (!isLast) {
          safe = lineEnd; // closing fence -> committable
          headingHold = -1; // the block a heading introduced is now committed
        }
      } else {
        inFence = true;
        fenceOpenAt = starts[i];
      }
      continue;
    }
    if (inFence) continue; // inside code -> not committable
    if (isLast) {
      const s = intraLineSafe(line); // trailing partial line -> commit whole sentences only
      if (s > 0 && tableStart < 0) {
        safe = starts[i] + s;
        headingHold = -1; // a real sentence after the heading committed -> join them
      }
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
      headingHold = -1; // the block a heading introduced is now committed
      continue;
    }
    // A heading commits WITH the block it introduces, never alone. toProse turns it into a
    // ":"-terminated lead-in that merges into the following sentence (one clip); committing it
    // early — its blank successor is a block boundary — would emit a lone "Heading:" clip in
    // streaming that the one-shot pass never produces (splitClips doesn't cut on ":"). Record its
    // start and clamp `safe` back to it below until a real (non-blank) line after it commits, so
    // streaming and one-shot agree.
    if (/^\s{0,3}#{1,6}\s/.test(line)) {
      if (headingHold < 0) headingHold = starts[i];
      continue;
    }
    // A newline-terminated prose line commits if the next line starts a new block/blank or
    // this line ends a sentence — otherwise it's a soft-wrapped continuation, so hold.
    if (BLOCK_START.test(next) || ENDS_LINE.test(line)) {
      safe = lineEnd;
      if (line.trim() !== "") headingHold = -1; // real content joined a pending heading
    }
  }
  // Never commit into an open code fence, a still-streaming table, or a dangling heading at the tail.
  if (inFence && fenceOpenAt >= 0 && safe > fenceOpenAt) safe = fenceOpenAt;
  if (tableStart >= 0 && safe > tableStart) safe = tableStart;
  if (headingHold >= 0 && safe > headingHold) safe = headingHold;
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
export function chunkStream(raw, flush, engine = "kokoro") {
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
