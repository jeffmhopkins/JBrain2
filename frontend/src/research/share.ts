// Public report-share URL helpers, isolated so the parse is unit-testable (App reads the live
// location; tests pass strings). Unlike the jcode share, the token IS the path (the owner chose
// a plain token-in-URL) — no #t= fragment, no redeem. The shell-less ResearchShareApp mounts on
// this path and fetches the report(s) straight from the public /api/research-share endpoint.

const SHARE_PATH = /^\/share\/([A-Za-z0-9_-]{1,128})\/?$/;

/** The public copy-link for a share token. */
export function researchShareUrl(token: string): string {
  return `${window.location.origin}/share/${encodeURIComponent(token)}`;
}

/** The share token if `pathname` is a report-share path (/share/{token}), else null. */
export function parseResearchSharePath(pathname: string = window.location.pathname): string | null {
  const match = SHARE_PATH.exec(pathname);
  return match ? (match[1] as string) : null;
}
