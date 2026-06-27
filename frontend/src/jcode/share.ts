// Share-link URL helpers, isolated so the parse is unit-testable (App reads the live
// location; tests pass strings). The secret rides the URL FRAGMENT (#t=…), which the
// browser never sends to the server — only the same-origin redeem POST carries it.

const SHARE_PATH = /^\/jcode\/s\/([A-Za-z0-9_-]{1,64})\/?$/;

/** The copy-link for a session + its share secret. */
export function shareUrl(sid: string, token: string): string {
  return `${window.location.origin}/jcode/s/${encodeURIComponent(sid)}#t=${encodeURIComponent(token)}`;
}

/** The session id if `pathname` is a share-link path (/jcode/s/{sid}), else null —
 * regardless of whether a secret is present. The scoped share app mounts on the PATH
 * alone, so a reload after the secret is stripped from the URL (or a re-open of an
 * already-claimed link) still lands on the session via the existing cookie instead of
 * falling through to the owner app's login. Defaults to the live location. */
export function parseSharePath(pathname: string = window.location.pathname): string | null {
  const match = SHARE_PATH.exec(pathname);
  return match ? (match[1] as string) : null;
}

/** If `pathname`+`hash` are a share link WITH a secret (/jcode/s/{sid}#t=token), the
 * {sid, token}; else null. The share app uses this to decide whether to redeem (a
 * secret present) or fall back to an existing scoped cookie (none). */
export function parseShareLink(
  pathname: string = window.location.pathname,
  hash: string = window.location.hash,
): { sid: string; token: string } | null {
  const sid = parseSharePath(pathname);
  if (!sid) return null;
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const token = params.get("t");
  return token ? { sid, token } : null;
}
