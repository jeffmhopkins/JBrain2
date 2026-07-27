// Per-folder "is this category collapsed?" markers for the Research Library's Reports
// tab, keyed by folder id (the sentinel UNGROUPED_KEY stands in for the trailing null
// bucket). Device-local, like the Tasks screen's tasks/collapsed markers: folding a
// folder only tucks it away here, and the choice survives a remount. Folders show only
// in browse mode (no active search), so collapse applies there — a search cuts across
// every folder into one flat list.

export const REPORTS_COLLAPSED_KEY = "jb.research.collapsedFolders";

/** The stable key for a bucket: the folder id, or a sentinel for the null (Ungrouped) bucket. */
export const UNGROUPED_KEY = "__ungrouped__";

export function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(REPORTS_COLLAPSED_KEY);
    return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
  } catch {
    return new Set();
  }
}

/** Persist the collapsed set (best-effort; a dropped write just re-expands next mount). */
export function writeCollapsed(collapsed: Set<string>): void {
  try {
    localStorage.setItem(REPORTS_COLLAPSED_KEY, JSON.stringify([...collapsed]));
  } catch {
    // best-effort — a folder that fails to persist simply reopens next mount
  }
}
