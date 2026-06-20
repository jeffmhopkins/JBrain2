package com.jbrain.dashboard

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class LocationReportTest {
    @Test
    fun encodesARequiredOwnTracksLocation() {
        val json = JSONObject(LocationReport(lat = 40.5, lon = -74.1, tst = 1_700_000_000).toJson())
        assertEquals("location", json.getString("_type"))
        assertEquals(40.5, json.getDouble("lat"), 0.0)
        assertEquals(-74.1, json.getDouble("lon"), 0.0)
        assertEquals(1_700_000_000L, json.getLong("tst"))
        // Optional fields are omitted (not null) when unknown.
        assertFalse(json.has("acc"))
        assertFalse(json.has("batt"))
        assertFalse(json.has("tid"))
    }

    @Test
    fun includesOptionalFieldsWhenKnown() {
        val json = JSONObject(
            LocationReport(1.0, 2.0, 100, accuracyM = 12, batteryPct = 88, trackerId = "ph").toJson(),
        )
        assertEquals(12, json.getInt("acc"))
        assertEquals(88, json.getInt("batt"))
        assertEquals("ph", json.getString("tid"))
    }
}
