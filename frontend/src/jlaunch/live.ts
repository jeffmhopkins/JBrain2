// URLs into a jlaunch job's live folder and its finished result. Plain same-origin links:
// the owner cookie authorises them, so a new tab or a download needs no fetch dance.

/** A live file: opened in a tab (the api serves it sandboxed) or, with `download`, saved. */
export function jlaunchLiveUrl(id: string, name: string, download = false): string {
  const base = `/api/jlaunch/jobs/${encodeURIComponent(id)}/live/${encodeURIComponent(name)}`;
  return download ? `${base}?download=1` : base;
}

/** A finished job's result (the spec's tarball), as a download. */
export function jlaunchResultUrl(id: string): string {
  return `/api/jlaunch/jobs/${encodeURIComponent(id)}/artifact`;
}

/** "about 1 h 20 min left" from seconds. */
export function fmtEta(s: number): string {
  if (s < 90) return "under 2 min left";
  const m = Math.round(s / 60);
  if (m < 60) return `about ${m} min left`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return `about ${h} h${r ? ` ${r} min` : ""} left`;
}
