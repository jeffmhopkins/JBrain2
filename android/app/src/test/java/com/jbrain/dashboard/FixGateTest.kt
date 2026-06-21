package com.jbrain.dashboard

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class FixGateTest {
    @Test
    fun acceptsFixesWithinTheGate() {
        assertTrue(FixGate.accept(0))
        assertTrue(FixGate.accept(10))
        assertTrue(FixGate.accept(FixGate.MAX_ACCURACY_M)) // exactly at the gate
    }

    @Test
    fun rejectsWideRadiusFixes() {
        assertFalse(FixGate.accept(FixGate.MAX_ACCURACY_M + 1))
        assertFalse(FixGate.accept(500))
    }

    @Test
    fun keepsUnknownAccuracy() {
        assertTrue(FixGate.accept(null))
    }
}
