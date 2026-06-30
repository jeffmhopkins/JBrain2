package com.jbrain.dashboard

/** How many pending fixes the offline queue holds before the oldest are dropped — the
 * size of the backfill buffer for a long stretch with no network (remote, in the
 * woods). The default ~5000 is ~7 h of dense moving fixes (5 s cadence) or weeks of
 * stationary heartbeats; raise it for longer off-grid trips at the cost of disk.
 *
 * Configurable at runtime via a stored override (e.g. a settings screen or `adb`),
 * resolved through this pure, clamped helper so an absurd value can't wedge the queue.
 * Pure, so it is JVM-unit-tested. */
object FixBufferConfig {
    const val DEFAULT = 5000

    // Floor keeps a useful backfill window even if misconfigured low; ceiling bounds
    // the on-disk file (the whole queue is rewritten on each mutation).
    const val MIN = 500
    const val MAX = 200_000

    /** The effective cap: the override when set, else the default, clamped to a sane
     * range. A null/zero/negative override means "unset" → the default. */
    fun resolve(override: Int?): Int {
        val requested = if (override == null || override <= 0) DEFAULT else override
        return requested.coerceIn(MIN, MAX)
    }
}
