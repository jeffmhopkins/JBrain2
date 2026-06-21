package com.jbrain.dashboard

import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin
import kotlin.math.sqrt

/** The location-update cadence asked of the provider: report at most every
 * [intervalMs] and only after [displacementM] of travel. */
data class RequestParams(val intervalMs: Long, val displacementM: Float)

enum class Motion { MOVING, STATIONARY }

/**
 * Decides the sampling cadence from the phone's recent movement so a moving device
 * gets a dense trail (5 s / 8 m) while a parked one relaxes to the heartbeat. The
 * framework displacement filter alone can't tell GPS jitter / slow drift from real
 * motion, so this adds hysteresis around a "still anchor": it takes [enterMovingM] of
 * travel to wake to MOVING, and [stillAfterMs] held within [stillRadiusM] to settle
 * back to STATIONARY — so a parked phone's jitter never flaps the state.
 *
 * Pure and framework-agnostic (no Android types), so it is JVM-unit-tested. Feed it
 * each ACCURACY-FILTERED fix; the returned params change only on a state transition,
 * which is the caller's cue to re-request location updates.
 */
class SamplingPolicy(
    val movingParams: RequestParams = RequestParams(5_000L, 8f),
    val stationaryParams: RequestParams = RequestParams(15 * 60 * 1000L, 25f),
    private val enterMovingM: Double = 30.0,
    private val stillRadiusM: Double = 20.0,
    private val stillAfterMs: Long = 120_000L,
) {
    var motion: Motion = Motion.MOVING
        private set

    // The point we measure stillness against, and when we began holding near it.
    private var anchorLat = 0.0
    private var anchorLon = 0.0
    private var anchorSinceMs = 0L
    private var hasAnchor = false

    /** Feed a fix (epoch ms); returns the desired request params for the resulting
     * motion state. The first fix assumes MOVING so the initial trail is dense. */
    fun onFix(lat: Double, lon: Double, atMs: Long): RequestParams {
        if (!hasAnchor) {
            resetAnchor(lat, lon, atMs)
            motion = Motion.MOVING
            return params()
        }
        val dist = haversineM(anchorLat, anchorLon, lat, lon)
        when {
            // Clearly left the anchor area — moving; re-anchor here.
            dist > enterMovingM -> {
                motion = Motion.MOVING
                resetAnchor(lat, lon, atMs)
            }
            // Drifting inside the dead-band: while moving, push the anchor forward so
            // the still-timer only accrues once genuinely settled. While stationary,
            // hold (hysteresis) — only enterMovingM wakes it.
            dist > stillRadiusM -> {
                if (motion == Motion.MOVING) resetAnchor(lat, lon, atMs)
            }
            // Held within the still radius: once long enough, settle to stationary.
            else -> {
                if (motion == Motion.MOVING && atMs - anchorSinceMs >= stillAfterMs) {
                    motion = Motion.STATIONARY
                }
            }
        }
        return params()
    }

    fun params(): RequestParams = if (motion == Motion.MOVING) movingParams else stationaryParams

    private fun resetAnchor(lat: Double, lon: Double, atMs: Long) {
        anchorLat = lat
        anchorLon = lon
        anchorSinceMs = atMs
        hasAnchor = true
    }

    private fun haversineM(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6_371_000.0 // mean Earth radius, metres
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = sin(dLat / 2) * sin(dLat / 2) +
            cos(Math.toRadians(lat1)) * cos(Math.toRadians(lat2)) * sin(dLon / 2) * sin(dLon / 2)
        return 2 * r * atan2(sqrt(a), sqrt(1 - a))
    }
}
