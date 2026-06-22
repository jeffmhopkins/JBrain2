package com.jbrain.dashboard

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalFixTest {
    @Test
    fun encodesThePagesLiveFeedShapeWithoutASubjectId() {
        val json = JSONObject(
            LocalFix(
                lat = 40.5, lon = -74.1, tst = 1_700_000_000,
                accuracyM = 12, velocityMps = 9.0, batteryPct = 88,
            ).toJson(),
        )
        assertEquals(40.5, json.getDouble("lat"), 0.0)
        assertEquals(-74.1, json.getDouble("lon"), 0.0)
        // ISO-8601 UTC, matching the live socket's captured_at.
        assertEquals("2023-11-14T22:13:20Z", json.getString("captured_at"))
        assertEquals(12, json.getInt("accuracy_m"))
        assertEquals(9.0, json.getDouble("velocity_mps"), 0.0)
        assertEquals(88, json.getInt("battery_pct"))
        // The page stamps the subject id (a loopback fix is always self), so it is absent here.
        assertEquals(false, json.has("subject_id"))
    }

    @Test
    fun keepsNullableKeysPresentAsNullWhenUnknown() {
        val json = JSONObject(LocalFix(lat = 1.0, lon = 2.0, tst = 100).toJson())
        assertTrue(json.has("accuracy_m") && json.isNull("accuracy_m"))
        assertTrue(json.has("velocity_mps") && json.isNull("velocity_mps"))
        assertTrue(json.has("battery_pct") && json.isNull("battery_pct"))
    }

    @Test
    fun busDeliversToTheRegisteredListenerAndStopsOnClear() {
        val seen = mutableListOf<LocalFix>()
        LocalFixBus.setListener { seen.add(it) }
        val fix = LocalFix(lat = 1.0, lon = 2.0, tst = 100)
        LocalFixBus.publish(fix)
        assertEquals(1, seen.size)
        assertSame(fix, seen[0])
        LocalFixBus.setListener(null)
        LocalFixBus.publish(fix) // no listener: dropped, not buffered
        assertEquals(1, seen.size)
    }
}
