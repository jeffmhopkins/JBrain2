// Client-side inert-rendering for jmolt's review queue (docs/plans/JMOLT_PLAN.md, M15).
//
// jmolt's staged writes are its own words and the third-party Moltbook content it reacted
// to — one hop from attacker-authorable text. Rendered as a React text child they are
// already HTML-escaped (no injection), so this adds the other two M15 defences: it strips
// invisible / bidi / zero-width / steganographic characters, and defangs URL schemes so a
// pasted link is not clickable or copy-pasteable-into-a-browser as-is. Mirrors the
// server-side `sanitize_for_owner` (jbrain.agent.jmolt_digest).

// Invisible / bidi / zero-width / tag / variation-selector ranges — the same set the
// backend blocks/strips (jmolt_guards._INVISIBLE_RANGES). Built from explicit codepoint
// escapes (never literal invisible characters in source) so the class is auditable.
const INVISIBLE = new RegExp(
  "[" +
    "\\u00AD" + // soft hyphen
    "\\u180E" + // Mongolian vowel separator
    "\\u200B-\\u200F" + // ZWSP..RLM
    "\\u202A-\\u202E" + // bidi embeddings / overrides
    "\\u2060-\\u2064" + // word joiner, invisible operators
    "\\u2066-\\u2069" + // bidi isolates
    "\\uFE00-\\uFE0F" + // variation selectors
    "\\uFEFF" + // BOM / ZWNBSP
    "\\uFFF9-\\uFFFB" + // interlinear annotation controls
    "\\u{E0000}-\\u{E007F}" + // Unicode Tag characters (ASCII smuggling)
    "\\u{E0100}-\\u{E01EF}" + // variation selectors supplement
    "]",
  "gu",
);

// Dangerous URL/script schemes, defanged to match the server sanitizer
// (jbrain.agent.jmolt_digest._SCHEME): http(s) → hxxp(s), others → x-<scheme>.
const SCHEME = /\b(https?|javascript|data|vbscript|file|mailto):/gi;

function defangScheme(_match: string, scheme: string): string {
  const s = scheme.toLowerCase();
  return s === "http" || s === "https" ? `${s.replace("http", "hxxp")}:` : `x-${s}:`;
}

/** Make jmolt-authored / third-party text safe to render inline: strip invisible and
 * bidirectional characters, and defang URL/script schemes so a link is neither clickable
 * nor a live payload. Returns plain text; the caller must render it as a text child
 * (never dangerouslySetInnerHTML). */
export function inertText(value: unknown): string {
  const s = value == null ? "" : String(value);
  return s.replace(INVISIBLE, "").replace(SCHEME, defangScheme);
}

/** A short human label for a staged outbox item's payload — its title or the head of its
 * content — already made inert. */
export function outboxPreview(payload: Record<string, unknown>): string {
  for (const key of ["title", "content", "description", "name", "post_id", "target_id"]) {
    const val = payload[key];
    if (typeof val === "string" && val.trim()) return inertText(val.trim()).slice(0, 280);
  }
  return "";
}
