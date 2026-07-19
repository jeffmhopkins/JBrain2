// Per-group "is this task group collapsed?" markers — device-local, like the
// per-task "viewed" markers and the theme / text-size prefs. Collapsing a group
// in the All view hides its cards behind its header; the choice is remembered
// across sessions on this device. Keyed by group id, with the trailing Ungrouped
// bucket stored under the literal "ungrouped".

export const TASKS_COLLAPSED_KEY = "jb.tasks.collapsedGroups";

export function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(TASKS_COLLAPSED_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

/** Persist the collapsed set (merging is unnecessary — the caller owns the full
 * set). Best-effort: a dropped write just re-expands the group next session. */
export function writeCollapsed(collapsed: Set<string>): void {
  try {
    localStorage.setItem(TASKS_COLLAPSED_KEY, JSON.stringify([...collapsed]));
  } catch {
    // best-effort; a dropped marker just re-expands the group
  }
}
