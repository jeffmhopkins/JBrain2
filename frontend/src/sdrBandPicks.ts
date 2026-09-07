// Which bands this device has actually tuned, so the picker can lead with them.
//
// Binding spec: `docs/mocks/band-recents/c-filter-first.html` **shape C**, chosen
// 2026-09-07. The table went from 36 sections to 57 in one change — every HF utility,
// broadcast and amateur band the receiver can reach — and the picker's order has always
// been the TABLE's order, which knows nothing about this box or who uses it.
//
// **Device-local, like the theme and the tasks/viewed markers**, and deliberately not a
// setting on the box: what an owner reaches for is a property of how they use this
// phone, it is worth nothing to anyone else, and a lost history costs one scroll rather
// than a broken screen. Every read and write is best-effort for the same reason —
// private mode, cleared site data and a browser that refuses storage all have to end in
// the plain list rather than a blank sheet.

/** Section id → when it was last picked, ISO. */
export type BandPicks = Record<string, string>;

export const BAND_PICKS_KEY = "jb.sdr.bandPicks";

/** How many picks are worth keeping. Two months of daily listening is far under this,
 *  and the cap is what stops a key that only ever grows: a section the owner tried once
 *  a year ago is not a recent band and is still in the list below, where it belongs. */
export const MAX_PICKS = 40;

export function loadPicks(): BandPicks {
  try {
    const raw = localStorage.getItem(BAND_PICKS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    // Filtered rather than trusted: this is JSON from disk, and one non-string value
    // would otherwise reach `Date` and sort the whole list into nonsense.
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).filter(
        ([, at]) => typeof at === "string" && !Number.isNaN(Date.parse(at)),
      ),
    ) as BandPicks;
  } catch {
    return {};
  }
}

/** Record that a section was chosen, and hand back the new map.
 *
 *  Returned as well as written because the sheet redraws from it immediately: reading
 *  it back would be a second parse of what we just serialised, and a write that failed
 *  would silently undo the reorder the owner just watched happen. */
export function notePick(id: string, now: Date = new Date()): BandPicks {
  const next = { ...loadPicks(), [id]: now.toISOString() };
  const trimmed = Object.fromEntries(
    Object.entries(next)
      .sort(([, a], [, b]) => Date.parse(b) - Date.parse(a))
      .slice(0, MAX_PICKS),
  );
  try {
    localStorage.setItem(BAND_PICKS_KEY, JSON.stringify(trimmed));
  } catch {
    // best-effort; a dropped pick costs this band its place at the top, nothing more
  }
  return trimmed;
}

/**
 * The sections picked most recently, newest first, at most `limit` of them.
 *
 * Pure, and it takes the sections rather than reading them: a pick for a section that
 * no longer exists — a band renamed or dropped from the table — must vanish from the
 * list rather than render a row with no band behind it.
 */
export function recentlyPicked<T extends { id: string }>(
  sections: readonly T[],
  picks: BandPicks,
  limit: number,
): T[] {
  const at = (section: T): number => Date.parse(picks[section.id] ?? "") || 0;
  return sections
    .filter((section) => picks[section.id] !== undefined)
    .sort((a, b) => at(b) - at(a))
    .slice(0, limit);
}
