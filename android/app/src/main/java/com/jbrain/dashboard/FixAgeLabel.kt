package com.jbrain.dashboard

/** The foreground-notification subtitle: a glanceable liveness cue from the age of
 * the last kept fix, so the owner can tell at a glance whether tracking is alive or
 * the OS has quietly suspended it (the on-device half of "is it failing in the bg").
 *
 * Coarse buckets only — a notification is not a clock — and a `STALE_MS` threshold
 * (twice the stationary heartbeat) past which it reads as a warning the owner should
 * act on (battery/background-location settings) rather than a fresh age. Pure, so it
 * is JVM-unit-tested. */
object FixAgeLabel {
    // Past this with no fix, treat tracking as stalled: 2x the 15-min heartbeat, so a
    // single missed heartbeat (indoors, brief outage) does not cry wolf.
    const val STALE_MS = 2 * 15 * 60 * 1000L

    /** Subtitle for a service that has not yet logged a fix this run. */
    fun acquiring(): String = "Locating…"

    /** Subtitle from the age of the last fix. Negative ages (clock skew) read as
     * just-now rather than a nonsensical future time. */
    fun forAge(ageMs: Long): String {
        if (ageMs >= STALE_MS) return "No fix in ${minutes(ageMs)} min — check battery & location settings"
        if (ageMs < 60_000L) return "Last fix just now"
        val mins = minutes(ageMs)
        return if (mins == 1L) "Last fix 1 min ago" else "Last fix $mins min ago"
    }

    private fun minutes(ageMs: Long): Long = (maxOf(0L, ageMs) / 60_000L)
}
