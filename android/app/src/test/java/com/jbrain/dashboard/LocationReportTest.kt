package com.jbrain.dashboard

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
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

    @Test
    fun fromJsonRoundTripsThroughTheQueueEncoding() {
        val report = LocationReport(40.5, -74.1, 1_700_000_000, accuracyM = 12)
        assertEquals(report, LocationReport.fromJson(report.toJson()))
    }

    @Test
    fun fromJsonRejectsNonLocationAndGarbage() {
        assertNull(LocationReport.fromJson("""{"_type":"transition","lat":1.0}"""))
        assertNull(LocationReport.fromJson("not json at all"))
    }

    @Test
    fun batchJsonEncodesAJsonArrayOldestFirst() {
        val arr = JSONArray(
            LocationReport.batchJson(
                listOf(LocationReport(1.0, 2.0, 10), LocationReport(3.0, 4.0, 20)),
            ),
        )
        assertEquals(2, arr.length())
        assertEquals(10L, arr.getJSONObject(0).getLong("tst"))
        assertEquals(20L, arr.getJSONObject(1).getLong("tst"))
    }

    @Test
    fun batchJsonOfEmptyIsAnEmptyArray() {
        assertEquals(0, JSONArray(LocationReport.batchJson(emptyList())).length())
    }
}
