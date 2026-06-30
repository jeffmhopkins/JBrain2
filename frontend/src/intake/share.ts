// Guided-intake share-link URL helpers, isolated so the parse is unit-testable. Mirrors
// the jcode share pattern: the secret rides the URL FRAGMENT (#t=…), which the browser
// never sends to the server — only the same-origin redeem POST carries it. The recipient
// app mounts on the PATH alone (/intake), so a reload after the secret is stripped still
// lands on the intake surface (its dead-link state) rather than the owner app.

const INTAKE_PATH = /^\/intake\/?$/;

/** The copy-link the owner sends out: /intake#t={secret}. */
export function intakeShareUrl(secret: string): string {
  return `${window.location.origin}/intake#t=${encodeURIComponent(secret)}`;
}

/** Whether `pathname` is the intake recipient surface (/intake), regardless of a secret. */
export function parseIntakePath(pathname: string = window.location.pathname): boolean {
  return INTAKE_PATH.test(pathname);
}

/** The share secret from the URL fragment (#t=…), or null when none is present. */
export function parseIntakeSecret(hash: string = window.location.hash): string | null {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  return params.get("t") || null;
}
