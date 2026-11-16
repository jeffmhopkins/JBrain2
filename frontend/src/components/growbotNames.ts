// A trained walk's library name is often a short name and a gist ("Master v3: turn, walk and
// back up, calmer backing"). The remote shows the short name, so a chip or row never runs off
// the card; the Walk with sheet shows the gist under it.

/** The part before the first colon, or the whole name when it has none. */
export function shortName(name: string): string {
  const at = name.indexOf(":");
  return at > 0 ? name.slice(0, at).trim() : name;
}

/** The part after the first colon, or "" when it has none. */
export function gistOf(name: string): string {
  const at = name.indexOf(":");
  return at > 0 ? name.slice(at + 1).trim() : "";
}
