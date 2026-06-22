package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LowPassFilterTest {
    private val tick = 100_000_000L // 0.1 s in nanoseconds

    @Test
    fun isEmptyBeforeAnySample() {
        assertNull(LowPassFilter(0.2).latest())
    }

    @Test
    fun firstSampleSeedsTheFilterExactly() {
        val f = LowPassFilter(0.2)
        assertEquals(3.0, f.update(3.0, 0L), 0.0)
        assertEquals(3.0, f.latest()!!, 0.0)
    }

    @Test
    fun laggingBehindAStepWhileApproachingIt() {
        // From a seeded 0, a sustained step to 10 should rise toward it but lag (the
        // point of smoothing) — one 0.1 s step into a 0.2 s constant moves ~1/3.
        val f = LowPassFilter(0.2)
        f.update(0.0, 0L)
        val after = f.update(10.0, tick)
        assertEquals(10.0 * (0.1 / (0.2 + 0.1)), after, 1e-9)
        assertTrue(after in 0.0..10.0)
    }

    @Test
    fun convergesTowardASustainedValue() {
        val f = LowPassFilter(0.2)
        f.update(0.0, 0L)
        var t = tick
        repeat(50) {
            f.update(10.0, t)
            t += tick
        }
        // After many time constants the smoothed value is essentially the input.
        assertEquals(10.0, f.latest()!!, 0.01)
    }

    @Test
    fun aNonAdvancingTimestampDoesNotMoveTheValue() {
        val f = LowPassFilter(0.2)
        f.update(4.0, tick)
        assertEquals(4.0, f.update(9.0, tick), 0.0) // dt = 0 -> alpha 0, unchanged
    }
}
