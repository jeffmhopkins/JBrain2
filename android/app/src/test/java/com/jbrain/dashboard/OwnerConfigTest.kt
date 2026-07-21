package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class OwnerConfigTest {
    @Test
    fun keepsRootPath() {
        assertEquals("https://host.example/", OwnerConfig.ownerUrl("https://host.example"))
    }

    @Test
    fun trimsTrailingSlashAndWhitespace() {
        assertEquals("https://host.example/", OwnerConfig.ownerUrl("  https://host.example/  "))
    }

    @Test
    fun rejectsNonHttpsBase() {
        assertThrows(IllegalArgumentException::class.java) {
            OwnerConfig.ownerUrl("http://host.example")
        }
    }

    @Test
    fun rejectsBlankBase() {
        assertThrows(IllegalArgumentException::class.java) { OwnerConfig.ownerUrl("   ") }
    }

    @Test
    fun validatesBase() {
        assertTrue(OwnerConfig.isValidBase("https://host.example"))
        assertTrue(OwnerConfig.isValidBase("  https://host.example/  "))
        assertFalse(OwnerConfig.isValidBase("http://host.example"))
        assertFalse(OwnerConfig.isValidBase("https://"))
        assertFalse(OwnerConfig.isValidBase(""))
    }
}
