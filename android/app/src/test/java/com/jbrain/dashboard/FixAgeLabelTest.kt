package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FixAgeLabelTest {
    @Test
    fun acquiringBeforeAnyFix() {
        assertEquals("Locating…", FixAgeLabel.acquiring())
    }

    @Test
    fun freshFixReadsJustNow() {
        assertEquals("Last fix just now", FixAgeLabel.forAge(0L))
        assertEquals("Last fix just now", FixAgeLabel.forAge(59_000L))
    }

    @Test
    fun singularAndPluralMinutes() {
        assertEquals("Last fix 1 min ago", FixAgeLabel.forAge(60_000L))
        assertEquals("Last fix 5 min ago", FixAgeLabel.forAge(5 * 60_000L))
    }

    @Test
    fun pastStaleThresholdWarns() {
        val label = FixAgeLabel.forAge(FixAgeLabel.STALE_MS)
        assertTrue(label, label.startsWith("No fix in"))
        assertTrue(label, label.contains("check battery & location settings"))
    }

    @Test
    fun negativeAgeFromClockSkewReadsJustNow() {
        assertEquals("Last fix just now", FixAgeLabel.forAge(-5_000L))
    }
}
