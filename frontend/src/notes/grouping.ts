// Day-grouping + relative-time helpers for the home stream.
// All math is in local time — "Today" means the user's today.

export interface DayGroup<T> {
  /** Local YYYY-MM-DD, stable for React keys. */
  key: string;
  label: string;
  items: T[];
}

export function dayKey(date: Date): string {
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${m}-${d}`;
}

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

export function dayLabel(date: Date, now: Date = new Date()): string {
  const diffDays = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  const opts: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric" };
  if (date.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return date.toLocaleDateString(undefined, opts);
}

/** Groups in input order; callers pass items oldest-first so groups read top-down. */
export function groupByDay<T>(
  items: readonly T[],
  getDate: (item: T) => Date,
  now: Date = new Date(),
): DayGroup<T>[] {
  const groups: DayGroup<T>[] = [];
  for (const item of items) {
    const date = getDate(item);
    const key = dayKey(date);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(item);
    else groups.push({ key, label: dayLabel(date, now), items: [item] });
  }
  return groups;
}

/**
 * Home-stream bound (docs/reference/DESIGN.md "Home stream"): true for local today and
 * the previous `days - 1` calendar days. Older notes live in Search.
 */
export function isWithinLastDays(date: Date, days: number, now: Date = new Date()): boolean {
  const diffDays = Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
  return diffDays < days;
}

/** "now" / "5m" / "3h" within 24h, otherwise local clock time. */
export function relativeTime(date: Date, now: Date = new Date()): string {
  const diffMs = now.getTime() - date.getTime();
  if (diffMs < 60 * 1000) return "now";
  if (diffMs < 60 * 60 * 1000) return `${Math.floor(diffMs / (60 * 1000))}m`;
  if (diffMs < DAY_MS) return `${Math.floor(diffMs / (60 * 60 * 1000))}h`;
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
