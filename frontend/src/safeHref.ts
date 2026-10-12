// URLs that arrive as data — LLM-extracted, fetched from the web, parsed out of an email,
// returned by a third-party API — are attacker-influenceable, and an `href` of
// `javascript:` / `data:` / `vbscript:` runs in the PWA's origin with the owner's cookie.
// React only warns about `javascript:` and never about `data:`, so every data-derived href
// goes through this allow-list. App-built paths (`/api/...`, blob: object URLs) don't: the
// app composes them, they are not data.

const ALLOWED = new Set(["http:", "https:", "mailto:"]);

/** The URL unchanged when it is an absolute http(s)/mailto URL, else `undefined` — the
 * caller then renders the text with no link. Parsed with `URL` rather than a prefix regex so
 * whitespace, control characters and case tricks are resolved exactly the way the browser
 * will resolve the same string in the href. */
export function safeHref(url: string | null | undefined): string | undefined {
  if (!url) return undefined;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return undefined;
  }
  return ALLOWED.has(parsed.protocol) ? url : undefined;
}
