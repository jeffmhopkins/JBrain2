package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Test

class SamplingPolicyTest {
    private val base = 40.0 to -74.0

    /** A point [metresNorth] north of base. Uses the nominal ~111,320 m/deg; the
     * policy's haversine yields ~111,195 m/deg, so true distances run ~0.1% under
     * the labels — every threshold below is chosen with margin, never on the edge. */
    private fun north(metresNorth: Double): Pair<Double, Double> =
        (base.first + metresNorth / 111_320.0) to base.second

    @Test
    fun startsMovingWithDenseParams() {
        val p = SamplingPolicy()
        val params = p.onFix(base.first, base.second, atMs = 0)
        assertEquals(Motion.MOVING, p.motion)
        assertEquals(p.movingParams, params)
        assertEquals(5_000L, params.intervalMs)
        assertEquals(8f, params.displacementM)
    }

    @Test
    fun settlesToStationaryAfterHoldingStill() {
        val p = SamplingPolicy()
        p.onFix(base.first, base.second, atMs = 0)
        // Same spot two minutes later -> stationary, with the relaxed params.
        val params = p.onFix(base.first, base.second, atMs = 120_000)
        assertEquals(Motion.STATIONARY, p.motion)
        assertEquals(p.stationaryParams, params)
    }

    @Test
    fun parkedJitterDoesNotFlapBackToMoving() {
        val p = SamplingPolicy()
        p.onFix(base.first, base.second, atMs = 0)
        p.onFix(base.first, base.second, atMs = 120_000) // -> STATIONARY
        // A 10 m jitter (inside the 20 m still radius) keeps it stationary.
        val (jLat, jLon) = north(10.0)
        p.onFix(jLat, jLon, atMs = 130_000)
        assertEquals(Motion.STATIONARY, p.motion)
    }

    @Test
    fun realMovementWakesItBackToMoving() {
        val p = SamplingPolicy()
        p.onFix(base.first, base.second, atMs = 0)
        p.onFix(base.first, base.second, atMs = 120_000) // -> STATIONARY
        // A 50 m jump exceeds the 30 m enter-moving threshold.
        val (mLat, mLon) = north(50.0)
        val params = p.onFix(mLat, mLon, atMs = 130_000)
        assertEquals(Motion.MOVING, p.motion)
        assertEquals(p.movingParams, params)
    }

    @Test
    fun staysMovingWhileTravelling() {
        val p = SamplingPolicy()
        var t = 0L
        // Each step is 50 m further north, 5 s apart — never settles.
        for (m in 0..200 step 50) {
            val (lat, lon) = north(m.toDouble())
            p.onFix(lat, lon, atMs = t)
            t += 5_000
        }
        assertEquals(Motion.MOVING, p.motion)
    }

    @Test
    fun slowDriftBeyondTheStillRadiusKeepsItMoving() {
        val p = SamplingPolicy()
        p.onFix(base.first, base.second, atMs = 0)
        // Creep 25 m (past the 20 m still radius, under the 30 m wake) every 90 s:
        // the still-timer keeps resetting, so it never settles to stationary.
        var t = 0L
        for (step in 1..4) {
            t += 90_000
            val (lat, lon) = north(25.0 * step)
            p.onFix(lat, lon, atMs = t)
        }
        assertEquals(Motion.MOVING, p.motion)
    }
}
