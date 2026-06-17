// Absolute, viewer-local time labels for Talk-board post signatures (mock B):
//   - today        → "today 9:14"
//   - this year    → "Mar 12"
//   - earlier      → "Mar 12, 2025"
// Off the post's ISO `created_at` (the server never localizes), so two devices in different
// timezones each read their own clock — matching how the rest of the app renders times.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function talkTime(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) {
    return `today ${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  const md = `${MONTHS[d.getMonth()]} ${d.getDate()}`;
  return d.getFullYear() === now.getFullYear() ? md : `${md}, ${d.getFullYear()}`;
}
