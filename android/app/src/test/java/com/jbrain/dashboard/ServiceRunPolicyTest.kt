package com.jbrain.dashboard

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ServiceRunPolicyTest {
    @Test
    fun runsOnlyWhenPairedAndPreciseLocationGranted() {
        assertTrue(ServiceRunPolicy.shouldRun(paired = true, hasFineLocation = true))
    }

    @Test
    fun staysOffWhenUnpaired() {
        assertFalse(ServiceRunPolicy.shouldRun(paired = false, hasFineLocation = true))
    }

    @Test
    fun staysOffWithoutPreciseLocation() {
        assertFalse(ServiceRunPolicy.shouldRun(paired = true, hasFineLocation = false))
        assertFalse(ServiceRunPolicy.shouldRun(paired = false, hasFineLocation = false))
    }
}
