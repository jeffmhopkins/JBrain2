// Per-task "have I opened the latest run's session?" markers, keyed task id →
// the started_at of the newest run opened on this device. Device-local (like the
// theme / text-size prefs): a task's result band — and the launcher's Tasks badge
// — read "new" until that task's latest run has been opened here. Opening the Tasks
// screen does not clear anything; only opening the session does. Shared by
// TasksScreen (the card band) and Launcher (the badge) so both count the same thing.
import type { Task } from "../api/client";

export const TASKS_VIEWED_KEY = "jb.tasks.viewedRunAt";

export function loadViewed(): Record<string, string> {
  try {
    const raw = localStorage.getItem(TASKS_VIEWED_KEY);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}

/** A task has an unviewed result when its latest run is newer than the last run
 * whose session was opened on this device (or none has been). */
export function isUnviewed(task: Task, viewed: Record<string, string>): boolean {
  const latest = task.latest_run;
  if (latest === null) return false;
  const seen = viewed[task.id];
  return seen === undefined || new Date(seen) < new Date(latest.started_at);
}

/** How many tasks carry an unviewed latest run — the launcher's Tasks badge. */
export function countUnviewed(tasks: Task[], viewed: Record<string, string>): number {
  return tasks.reduce((n, t) => n + (isUnviewed(t, viewed) ? 1 : 0), 0);
}

/** Record that a task's latest run has been opened on this device. Writes straight
 * to localStorage (merging the current map) — callers persist here rather than from
 * a React state updater because opening a session unmounts the Tasks screen and a
 * queued updater on an unmounting component never runs. */
export function writeViewed(taskId: string, startedAt: string): void {
  try {
    localStorage.setItem(
      TASKS_VIEWED_KEY,
      JSON.stringify({ ...loadViewed(), [taskId]: startedAt }),
    );
  } catch {
    // best-effort; a dropped marker just re-shows the band/badge as "new"
  }
}
