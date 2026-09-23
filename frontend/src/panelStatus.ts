/** What a panel's last report MEANS, separated from how it is drawn.
 *
 * The box already knew all of this and the only reader was `grep` over its structured log,
 * reachable through the debug API and a terminal. The owner has neither (CLAUDE.md #10), so
 * "is her panel alive, and did the update land" was a question only a shell could answer —
 * which is what "just your update only has 0.2.88" cost, on a panel that had in fact updated
 * forty minutes earlier.
 *
 * Pure, and its own file, because the rules below are judgements rather than rendering: how
 * long a silence has to be before it means something, and which of two dozen reported numbers
 * are worth a line on a phone. Those are the parts worth holding to a test.
 */

/** A panel reports every ~15 minutes (`main.c`). */
export const PANEL_REPORT_S = 15 * 60;

export type PanelHealth =
  /** Reported within a cycle or two. */
  | "ok"
  /** Has missed cycles but not enough to be alarming — a Wi-Fi blip looks like this. */
  | "late"
  /** Long enough that something is wrong: powered off, off the network, or wedged. */
  | "silent"
  /** Flashed and never heard from. A different fault from having gone quiet, with a
   *  different first move — check it was provisioned against this box at all. */
  | "never";

/** `age_s` is computed on the BOX, not from a phone clock: the two disagree by minutes on a
 *  phone that has been asleep, and "last seen 4 minutes in the future" is how a working fleet
 *  looks broken. A negative age means no report has ever arrived. */
export function panelHealth(ageS: number): PanelHealth {
  if (ageS < 0) return "never";
  // Two missed cycles before it is even "late": one missed report is an ordinary Wi-Fi blip
  // on a radio in a bedroom, and a fleet view that cries wolf gets ignored exactly when it
  // matters.
  if (ageS <= PANEL_REPORT_S * 2) return "ok";
  if (ageS <= PANEL_REPORT_S * 8) return "late";
  return "silent";
}

/** "4 min ago", "2 hours ago", "3 days ago" — coarse on purpose. Nobody needs the seconds,
 *  and a precise number invites reading a meaning into the difference between 61 and 64. */
export function agoLabel(ageS: number): string {
  if (ageS < 0) return "never";
  if (ageS < 90) return "just now";
  const mins = Math.round(ageS / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(ageS / 3600);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return `${Math.round(ageS / 86400)} days ago`;
}

/** The report, as the panel spelled it. Every field is optional because it is a snapshot of
 *  whatever firmware that panel happens to be running — an older one simply says less, and a
 *  fleet view that broke on a panel mid-upgrade would be useless during exactly the event it
 *  exists to watch. */
export interface PanelReport {
  version?: string;
  uptime_ms?: number;
  reset_reason?: string;
  restart_why?: string;
  screen?: string;
  blit_ok?: number;
  blit_fail?: number;
  blit_fail_total?: number;
  blit_recov?: number;
  int_largest?: number;
  free_heap?: number;
  wifi_drops?: number;
  wifi_reason?: number;
  ota_err?: string;
  ota_tries?: number;
  mic_peak?: number;
  vocab_bad?: number;
  crash_phase?: number;
  panel_reset?: boolean;
}

/** A line of plain words for what the panel is doing right now. Facts, not judgements — the
 *  judgements are `panelConcerns`. */
export function panelFacts(report: PanelReport): string[] {
  const out: string[] = [];
  // FIRST, BECAUSE IT IS THE ONE THAT PREVENTS A WRONG DIAGNOSIS. A sleeping screen stops
  // blitting on purpose, so `blit_ok` stops climbing — the exact signature of the stalled
  // render task that cost 0.2.44 a photograph from the owner to diagnose.
  if (report.screen) out.push(`screen ${report.screen}`);
  if (typeof report.uptime_ms === "number" && report.uptime_ms > 0) {
    const hours = report.uptime_ms / 3_600_000;
    out.push(
      hours < 1
        ? `up ${Math.max(1, Math.round(report.uptime_ms / 60_000))} min`
        : `up ${Math.round(hours)}h`,
    );
  }
  if (report.restart_why) out.push(`last restart: ${report.restart_why}`);
  else if (report.reset_reason) out.push(`last boot: ${report.reset_reason}`);
  // A FACT, NOT AN ALARM, and the difference matters. The backend's own note says a dead
  // capture path is zero "across several reports while someone is talking near it" — and a
  // snapshot cannot see either half. A panel alone in a dark bedroom overnight legitimately
  // hears nothing, so making this red would paint the card every morning, and a fleet view
  // that cries wolf is ignored exactly when it matters.
  if (report.mic_peak === 0) out.push("heard nothing since the last report");
  return out;
}

/** What is worth saying out loud, worst first. Empty means nothing to report, which is the
 *  answer the owner is usually looking for and should not have to infer from a wall of
 *  numbers. */
export function panelConcerns(report: PanelReport): string[] {
  const out: string[] = [];
  // AN UPDATE THAT CANNOT INSTALL RETRIES FOREVER REPORTING THE OLD VERSION, which from the
  // box is indistinguishable from a panel nobody offered an update to. It goes first.
  if (report.ota_err) {
    out.push(
      `update failing: ${report.ota_err}${report.ota_tries ? ` (${report.ota_tries} tries)` : ""}`,
    );
  }
  if ((report.blit_fail_total ?? 0) > 0) {
    const recov = report.blit_recov ?? 0;
    out.push(
      `${report.blit_fail_total} failed frames${recov > 0 ? `, ${recov} self-heals` : ""} — the display fault`,
    );
  }
  // -1 is a cold boot, which is the healthy value. Anything else is the render stage it
  // reached before the last restart, and it is the only substitute this panel has for a
  // backtrace on a console it does not own.
  if (typeof report.crash_phase === "number" && report.crash_phase >= 0) {
    out.push(`restarted from render stage ${report.crash_phase}`);
  }
  if ((report.wifi_drops ?? 0) > 2) {
    out.push(
      `${report.wifi_drops} Wi-Fi drops${report.wifi_reason ? ` (reason ${report.wifi_reason})` : ""}`,
    );
  }
  if ((report.vocab_bad ?? 0) > 0) out.push(`${report.vocab_bad} commands it cannot hear`);
  // `panel_reset` is NOT here, deliberately. False means the display controller got only a
  // software reset at boot — the state the post-OTA black screen lives in — but the field
  // defaults to false on the box, so firmware too old to send it reports false and would be
  // flagged for a fault it may not have. The condition it points at shows up in the failed
  // frames above when it actually bites, and a red line nobody can act on is the kind that
  // teaches an owner to stop reading the card.
  return out;
}
