/** The recordings surface's arithmetic, away from the components that draw it.
 *
 *  Binding specs: `docs/mocks/recording/d-trim-sheet.html` (the trim sheet) and
 *  `docs/mocks/recording/a-tape-deck.html` (the capture control and the library rows).
 *  Three surfaces read these numbers — the Record button's running size, the library
 *  row's duration/size column, and the trim sheet's live "frees N" — so the conversion
 *  between "a selection" and "what it costs" is one function rather than one per screen.
 *
 *  Modelled on `sdrBandwidth.ts`, and for the same reason: the trim sheet has three ways
 *  into one pair of numbers (a drag, arrow keys, per-frame nudge buttons), and a copy of
 *  the clamping in each of them is three chances for the handles to disagree about where
 *  the end of the clip is.
 */

/** An MP3 frame is 1152 samples; at the sidecar's 16 kHz that is 72 ms
 *  (`deploy/sdr/listen.py`, `docs/plans/SDR_RECORDING_PLAN.md` §2). A trim done with
 *  `ffmpeg -c copy` copies frames rather than re-encoding them, so it is lossless and
 *  instant — and it lands on a frame boundary. This is the smallest honest unit the
 *  format has, and DESIGN.md's destructive-editing rule 4 forbids implying a finer one.
 */
export const FRAME_MS = 72;
export const FRAME_S = FRAME_MS / 1000;

/** What one second of the sidecar's stream costs: 64 kbps mono MP3 is 8 kB/s,
 *  480 kB/minute, 28.8 MB/hour (`listen.py` AUDIO_BITRATE_BPS). Used only where there
 *  is no measured row to divide — a capture in flight whose bytes the box has not
 *  reported yet. Everywhere else, `bytesPerSecond` asks the clip itself. */
export const NOMINAL_BYTES_PER_S = 8000;

/** The shortest selection the sheet will hold. Half a second rather than one frame:
 *  the handles are 34 px of fingertip on a 118 px-tall picture, and a selection that
 *  can collapse to 72 ms turns an over-drag into a clip with nothing in it. */
export const MIN_SELECTION_S = 0.5;

export interface Selection {
  startS: number;
  endS: number;
}

export type Handle = "start" | "end";

/** The nearest frame boundary. Every position the sheet publishes goes through here,
 *  so the readout, the aria value and the seconds sent to the server all name a cut the
 *  server can actually make. */
export function snapToFrame(seconds: number): number {
  return Math.round(seconds / FRAME_S) * FRAME_S;
}

/** A selection made legal: snapped to frames, inside the clip, and no narrower than
 *  `MIN_SELECTION_S`.
 *
 *  Clamped HERE and only here. A finger past the end of the picture is asking for the
 *  end of the clip, not for a refusal — the same reasoning as `snapBandwidth`.
 *
 *  **The clip's own end is exempt from the grid.** 0 and `totalS` are where the file
 *  already begins and ends, so reaching them needs no cut and no rounding; snapping the
 *  end DOWN would open every sheet on a selection a frame short of the whole clip, which
 *  reads as a trim the owner did not ask for and offers to free a few hundred bytes for
 *  destroying the original. */
export function clampSelection(selection: Selection, totalS: number): Selection {
  // A clip shorter than the minimum selection is still trimmable in principle, and
  // pretending otherwise would produce a start past its own end.
  const span = Math.min(MIN_SELECTION_S, totalS);
  // Bounding the START at `totalS - span` first is what keeps the min-gap push below
  // from ever running the end past the clip, so there is no second correction pass.
  const start = Math.min(Math.max(snapToFrame(selection.startS), 0), Math.max(totalS - span, 0));
  const wanted = selection.endS >= totalS ? totalS : Math.min(snapToFrame(selection.endS), totalS);
  return { startS: start, endS: Math.max(wanted, start + span) };
}

/** Move one handle to an absolute position — what a drag or a tap on the waveform asks
 *  for. The other handle never moves: pushing the far edge along would let a drag past
 *  it silently shorten the clip from the end the finger is nowhere near. */
export function moveHandle(
  selection: Selection,
  handle: Handle,
  toS: number,
  totalS: number,
): Selection {
  const span = Math.min(MIN_SELECTION_S, totalS);
  if (handle === "start") {
    return clampSelection({ ...selection, startS: Math.min(toS, selection.endS - span) }, totalS);
  }
  return clampSelection({ ...selection, endS: Math.max(toS, selection.startS + span) }, totalS);
}

/** Move one handle BY an amount — the nudge buttons (±1 frame) and the arrow keys. */
export function nudgeHandle(
  selection: Selection,
  handle: Handle,
  byS: number,
  totalS: number,
): Selection {
  const from = handle === "start" ? selection.startS : selection.endS;
  return moveHandle(selection, handle, from + byS, totalS);
}

/** Bytes per second of THIS clip, measured rather than assumed.
 *
 *  The nominal 8 kB/s is what the encoder is configured for, but a stored file carries
 *  its own header and the last frame is whatever it is — so a 42-second clip is not
 *  exactly 336 kB. The sheet's whole argument is "trimming frees this much", and a
 *  figure derived from the file the owner is looking at is the one that survives
 *  comparison with the size printed on its own row. */
export function bytesPerSecond(bytes: number, durationS: number): number {
  return durationS > 0 && bytes > 0 ? bytes / durationS : NOMINAL_BYTES_PER_S;
}

export interface TrimGain {
  keptS: number;
  discardedS: number;
  keptBytes: number;
  freedBytes: number;
}

/** What a selection keeps and what committing it would free. The live line under the
 *  waveform ("Discards 1:05 of dead air — frees 509 kB") is this, and DESIGN.md's rule 5
 *  is why it is computed rather than implied: the whole reason the feature exists is
 *  disk. */
export function trimGain(selection: Selection, totalS: number, totalBytes: number): TrimGain {
  const rate = bytesPerSecond(totalBytes, totalS);
  const keptS = Math.max(0, selection.endS - selection.startS);
  const discardedS = Math.max(0, totalS - keptS);
  const keptBytes = Math.round(keptS * rate);
  return {
    keptS,
    discardedS,
    keptBytes,
    // Against the file's REAL size, not against kept×rate rounded twice — otherwise a
    // selection covering the whole clip reports a few bytes freed by arithmetic alone.
    freedBytes: Math.max(0, totalBytes - keptBytes),
  };
}

/** Whether this selection is still the whole clip — within one frame, because that is
 *  the resolution the handles have. The sheet says "Nothing trimmed yet" rather than
 *  offering to free nothing. */
export function isWholeClip(selection: Selection, totalS: number): boolean {
  return selection.startS <= FRAME_S && selection.endS >= totalS - FRAME_S;
}

/** A length as the library and the sheet print it: `m:ss`. */
export function formatDuration(seconds: number): string {
  const whole = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** A handle's position, `m:ss.d`. The tenth is there because the handles move in
 *  frames — 72 ms — and `m:ss` would show two distinct positions as the same number. */
export function formatStamp(seconds: number): string {
  const at = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  return `${formatDuration(at)}.${Math.floor((at % 1) * 10)}`;
}

const KB = 1024;
const MB = 1024 * 1024;
const GB = 1024 * 1024 * 1024;

/** A size as every one of these surfaces prints it: `509 kB`, `1.4 MB`, `1.9 GB`.
 *
 *  One function for the clip sizes and for the header's usage meter, because they are
 *  read against each other — "1.9 GB of 8 GB" over a list of MB is only legible if the
 *  two are the same scale. */
export function formatSize(bytes: number): string {
  const at = Math.max(0, Number.isFinite(bytes) ? bytes : 0);
  if (at >= GB) return `${(at / GB).toFixed(1)} GB`;
  if (at >= MB) return `${(at / MB).toFixed(1)} MB`;
  return `${Math.round(at / KB)} kB`;
}

/** The day a recording is filed under: `Today`, `Yesterday`, or `7 Sep`.
 *
 *  Local time on purpose. The row's clock is local, so a UTC-derived heading would put
 *  a 23:45 recording under yesterday's date on a westerly box — the one case where the
 *  grouping is read most closely. */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "Unknown";
  const midnight = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(now) - midnight(at)) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return at.toLocaleDateString([], { day: "numeric", month: "short" });
}

/** The wall-clock time on a row: `23:45`. */
export function clockLabel(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** The library, cut into day sections newest-first — the shape the list renders.
 *
 *  Sorting here rather than trusting the server's order: the rows arrive newest-first,
 *  but a clip that just landed is unshifted locally, and one bad order puts a second
 *  "Today" heading halfway down the list. */
export function groupByDay<T extends { id: string; started_at: string }>(
  rows: readonly T[],
  now: Date = new Date(),
): { day: string; rows: T[] }[] {
  const sorted = [...rows].sort(
    (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime(),
  );
  const out: { day: string; rows: T[] }[] = [];
  for (const row of sorted) {
    const day = dayLabel(row.started_at, now);
    const last = out[out.length - 1];
    if (last && last.day === day) last.rows.push(row);
    else out.push({ day, rows: [row] });
  }
  return out;
}

/** The header's right-hand line. At rest it states the policy — **nothing expires**,
 *  there is no retention prune, so the only honest thing to say is that these are kept
 *  until the owner removes them. Once trimming has actually reclaimed something, it
 *  reports that instead: the argument for trimming is only credible if the box keeps
 *  score (DESIGN.md, "Sizes are always visible"). */
export function reclaimedLine(reclaimedBytes: number): string {
  return reclaimedBytes > 0
    ? `${formatSize(reclaimedBytes)} reclaimed by trimming`
    : "kept until you delete them";
}

/** The header's left-hand line: what the recordings occupy, and out of what when the
 *  box has said how big its disk is. Degrades rather than inventing a denominator — a
 *  meter with a made-up total is worse than a plain number, and the api's `usage` is
 *  only required to carry the bytes and the count. */
export function usageLine(bytes: number, count: number, diskTotalBytes?: number | null): string {
  if (diskTotalBytes && diskTotalBytes > 0) {
    return `${formatSize(bytes)} of ${formatSize(diskTotalBytes)}`;
  }
  return `${formatSize(bytes)} in ${count} recording${count === 1 ? "" : "s"}`;
}

/** How full the meter's bar is drawn, 0..1, or null when there is nothing to draw it
 *  against. Null is a bar that is not rendered at all, not a bar at zero. */
export function usageFraction(bytes: number, diskTotalBytes?: number | null): number | null {
  if (!diskTotalBytes || diskTotalBytes <= 0) return null;
  return Math.min(1, Math.max(0, bytes / diskTotalBytes));
}

/** The stored level envelope resampled to `count` bars, each 0..1.
 *
 *  The row carries whatever the box computed at stop (`peaks` on the row), which is not
 *  the number of bars a 390 px-wide sheet draws. Resampling by MAX rather than by mean:
 *  the picture's job is to show where the signal is against the dead air, and averaging
 *  a burst into its surrounding silence is exactly how a short transmission disappears
 *  from the waveform the owner is trimming against. */
export function waveformBars(peaks: readonly number[], count: number): number[] {
  if (count <= 0) return [];
  if (peaks.length === 0) return new Array(count).fill(0);
  return Array.from({ length: count }, (_unused, i) => {
    const from = Math.floor((i * peaks.length) / count);
    const to = Math.max(from + 1, Math.floor(((i + 1) * peaks.length) / count));
    let top = 0;
    for (let at = from; at < to && at < peaks.length; at++) {
      top = Math.max(top, Math.min(1, Math.max(0, peaks[at] ?? 0)));
    }
    return top;
  });
}
