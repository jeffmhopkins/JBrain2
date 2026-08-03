// Public results-share URL helpers, isolated so the parse is unit-testable (App reads the
// live location; tests pass strings). Like the research share, the token IS the path (a
// plain token-in-URL) — no fragment, no redeem. The shell-less JlaunchShareApp mounts on
// this path and fetches the run from the public /api/jlaunch-share endpoint.

const SHARE_PATH = /^\/results\/([A-Za-z0-9_-]{1,128})\/?$/;

/** The public copy-link for a results-share token. */
export function jlaunchShareUrl(token: string): string {
  return `${window.location.origin}/results/${encodeURIComponent(token)}`;
}

/** The public download URL for a shared run's artifact. */
export function jlaunchArtifactUrl(token: string): string {
  return `/api/jlaunch-share/${encodeURIComponent(token)}/artifact`;
}

/** The share token if `pathname` is a results-share path (/results/{token}), else null. */
export function parseJlaunchSharePath(pathname: string = window.location.pathname): string | null {
  const match = SHARE_PATH.exec(pathname);
  return match ? (match[1] as string) : null;
}
