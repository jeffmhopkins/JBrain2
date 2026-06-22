package com.jbrain.dashboard

/** A first-order (exponential) low-pass filter with a fixed time constant.
 *
 * Smooths a stream of timestamped samples so a noisy signal (here the phone's
 * acceleration magnitude) settles over ~[tauSeconds] rather than jumping on every
 * reading. The blend factor is derived from the real gap between samples, so an
 * irregular sensor cadence still filters to the same time constant. Pure math (no
 * Android types), so it is JVM-unit-tested. */
class LowPassFilter(private val tauSeconds: Double) {
    private var value: Double? = null
    private var lastNanos: Long = 0L

    /** Blend [sample] (taken at [atNanos], e.g. `SensorEvent.timestamp`) into the
     * running value and return it. The first sample seeds the filter; each later one
     * moves the value by `dt / (tau + dt)` of the way toward it. A non-advancing
     * timestamp contributes nothing (alpha 0) rather than dividing by zero. */
    fun update(sample: Double, atNanos: Long): Double {
        val prev = value
        if (prev == null) {
            value = sample
            lastNanos = atNanos
            return sample
        }
        val dt = (atNanos - lastNanos) / 1_000_000_000.0
        lastNanos = atNanos
        val alpha = if (dt <= 0.0) 0.0 else dt / (tauSeconds + dt)
        val next = prev + alpha * (sample - prev)
        value = next
        return next
    }

    /** The latest smoothed value, or null before the first sample. */
    fun latest(): Double? = value
}
