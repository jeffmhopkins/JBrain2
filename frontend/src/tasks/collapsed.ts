// Per-group "is this category collapsed?" markers, keyed by bucket id (the sentinel
// UNGROUPED_KEY stands in for the trailing null bucket). Device-local, like the
// per-task "viewed" markers and the theme / text-size prefs: collapsing a category
// on the Tasks screen only folds it away here, and the choice survives a remount.
// Only the "All" view shows the group headers, so collapse applies there — a single
// filtered group is already the whole view.

export const TASKS_COLLAPSED_KEY = "jb.tasks.collapsedGroups";

/** The stable key for a bucket: the group id, or a sentinel for the null bucket. */
export const UNGROUPED_KEY = "__ungrouped__";

export function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(TASKS_COLLAPSED_KEY);
    return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
  } catch {
    return new Set();
  }
}

/** Persist the collapsed set (best-effort; a dropped write just re-expands). */
export function writeCollapsed(collapsed: Set<string>): void {
  try {
    localStorage.setItem(TASKS_COLLAPSED_KEY, JSON.stringify([...collapsed]));
  } catch {
    // best-effort — a category that fails to persist simply reopens next mount
  }
}
