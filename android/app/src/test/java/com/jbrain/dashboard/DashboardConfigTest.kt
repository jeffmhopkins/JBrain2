package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class DashboardConfigTest {
    @Test
    fun appendsDashPath() {
        assertEquals("https://host.example/dash", DashboardConfig.dashboardUrl("https://host.example"))
    }

    @Test
    fun trimsTrailingSlashAndWhitespace() {
        assertEquals(
            "https://host.example/dash",
            DashboardConfig.dashboardUrl("  https://host.example/  "),
        )
    }

    @Test
    fun rejectsNonHttpsBase() {
        assertThrows(IllegalArgumentException::class.java) {
            DashboardConfig.dashboardUrl("http://host.example")
        }
    }

    @Test
    fun rejectsBlankBase() {
        assertThrows(IllegalArgumentException::class.java) { DashboardConfig.dashboardUrl("   ") }
    }
}
