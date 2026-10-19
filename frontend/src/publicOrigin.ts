// The address links that leave the app start with (OWNER_TUNNEL_PLAN §3.6). A phone reaches
// the box at a private origin a recipient could never open, so share links use the owner's
// public base URL once it is loaded, and the page's own origin until then or without one.

// No import of the API client: it builds share links with publicOrigin(), so the loader is
// handed its fetch instead of importing it back.
let configured: string | null = null;

export function publicOrigin(): string {
  return configured || window.location.origin;
}

/** Called after a save, so the next link built uses the new address without a reload. */
export function setPublicOrigin(url: string | null): void {
  configured = url ? url.replace(/\/+$/, "") : null;
}

/** Once per sign-in. A failure keeps the page's origin: a link that works on the home
 *  network beats no link. */
export async function loadPublicOrigin(
  fetchSetting: () => Promise<{ url: string; env_url: string }>,
): Promise<void> {
  try {
    const s = await fetchSetting();
    setPublicOrigin(s.url || s.env_url || null);
  } catch {
    // keep the fallback
  }
}
