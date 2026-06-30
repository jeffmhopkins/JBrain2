package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Test

class FixBufferConfigTest {
    @Test
    fun unsetOverrideUsesTheDefault() {
        assertEquals(FixBufferConfig.DEFAULT, FixBufferConfig.resolve(null))
        assertEquals(FixBufferConfig.DEFAULT, FixBufferConfig.resolve(0))
        assertEquals(FixBufferConfig.DEFAULT, FixBufferConfig.resolve(-1))
    }

    @Test
    fun aValidOverrideIsHonored() {
        assertEquals(20_000, FixBufferConfig.resolve(20_000))
    }

    @Test
    fun overrideIsClampedToSaneBounds() {
        assertEquals(FixBufferConfig.MIN, FixBufferConfig.resolve(1))
        assertEquals(FixBufferConfig.MAX, FixBufferConfig.resolve(10_000_000))
    }
}
