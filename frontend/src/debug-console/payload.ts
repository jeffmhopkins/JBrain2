// Decodes the debug-token payload the owner mints in the PWA — a
// base64url(JSON{v,u,k}) string carrying the box URL (`u`) and bearer key (`k`).
// The console reads it from the page's URL fragment (never sent to the server) or
// a paste box, and uses it to call /api/debug/* with `Authorization: Bearer <key>`.

export interface DebugToken {
  base: string;
  key: string;
}

export function decodeToken(payload: string): DebugToken | null {
  const p = payload.trim().replace(/^#/, "");
  if (!p) return null;
  try {
    const b64 = p.replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const parsed: unknown = JSON.parse(atob(padded));
    if (parsed && typeof parsed === "object") {
      const { u, k } = parsed as { u?: unknown; k?: unknown };
      if (typeof u === "string" && typeof k === "string" && u && k) {
        return { base: u.replace(/\/+$/, ""), key: k };
      }
    }
    return null;
  } catch {
    return null;
  }
}
