// Share-link URL helpers, isolated so the parse is unit-testable (App reads the live
// location; tests pass strings). The secret rides the URL FRAGMENT (#t=…), which the
// browser never sends to the server — only the same-origin redeem POST carries it.

const SHARE_PATH = /^\/jcode\/s\/([A-Za-z0-9_-]{1,64})\/?$/;

/** The copy-link for a session + its share secret. */
export function shareUrl(sid: string, token: string): string {
  return `${window.location.origin}/jcode/s/${encodeURIComponent(sid)}#t=${encodeURIComponent(token)}`;
}

/** If `pathname`+`hash` are a share link (/jcode/s/{sid}#t=token), the {sid, token};
 * else null. Defaults to the live location so App can call it bare on boot. */
export function parseShareLink(
  pathname: string = window.location.pathname,
  hash: string = window.location.hash,
): { sid: string; token: string } | null {
  const match = SHARE_PATH.exec(pathname);
  if (!match) return null;
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const token = params.get("t");
  return token ? { sid: match[1] as string, token } : null;
}
