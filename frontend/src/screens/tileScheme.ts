// Basemap tile scheme + URL helpers, kept side-effect free (no Leaflet import) so
// the interactive maps (leafletMap.ts) and the inline tool-view thumbnails
// (agent/views/locationMap.ts) share one source of truth — including the
// cache-busting version token.

export type TileScheme = "dark" | "light";

const TILE_SCHEME_KEY = "jbrain.map.tileScheme";
const DEFAULT_TILE_SCHEME: TileScheme = "dark";

// Bump to force every client to refetch tiles. The token is part of the request
// URL (the browser/WebView's cache key), so a new value sidesteps the 30-day tile
// cache with no UI and no app update — clients pick it up on the next load of the
// new frontend bundle. Use this when an upstream style changes but the scheme path
// stays the same (otherwise clients would serve stale tiles until the cache ages).
export const TILE_CACHE_VERSION = "1";

/** The proxy URL template for a scheme, carrying the cache-bust token. */
export function tileUrl(scheme: TileScheme): string {
  return `/api/tiles/${scheme}/{z}/{x}/{y}.png?v=${TILE_CACHE_VERSION}`;
}

/** The owner's last basemap choice, persisted so a reload (or a tab/app switch)
 * keeps it. Defaults to dark — matching the app UI — and tolerates a missing or
 * blocked localStorage (private mode / WebView). */
export function readTileScheme(): TileScheme {
  try {
    return localStorage.getItem(TILE_SCHEME_KEY) === "light" ? "light" : DEFAULT_TILE_SCHEME;
  } catch {
    return DEFAULT_TILE_SCHEME;
  }
}

export function writeTileScheme(scheme: TileScheme): void {
  try {
    localStorage.setItem(TILE_SCHEME_KEY, scheme);
  } catch {
    // A blocked store just means the choice isn't remembered — never a crash.
  }
}
