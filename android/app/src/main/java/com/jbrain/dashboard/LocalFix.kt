package com.jbrain.dashboard

import java.time.Instant
import org.json.JSONObject

/** This phone's own location fix, shaped for the dashboard page's live-feed handler
 * (`window.__jbrainLocalFix`) so the map can move the self-pin instantly. The page
 * stamps the subject id (a loopback fix is always the viewer's own device), so only
 * the position fields travel. Encoding is pure (org.json + java.time), so it is
 * JVM-unit-tested. */
data class LocalFix(
    val lat: Double,
    val lon: Double,
    val tst: Long, // capture instant, Unix epoch seconds
    val accuracyM: Int? = null,
    val velocityMps: Double? = null,
    val batteryPct: Int? = null,
) {
    fun toJson(): String {
        val o = JSONObject()
        o.put("lat", lat)
        o.put("lon", lon)
        // ISO-8601 UTC, matching the live socket's `captured_at` so both feeds parse alike.
        o.put("captured_at", Instant.ofEpochSecond(tst).toString())
        // Keep the keys present with explicit null when unknown (the page reads nullable fields).
        o.put("accuracy_m", accuracyM ?: JSONObject.NULL)
        o.put("velocity_mps", velocityMps ?: JSONObject.NULL)
        o.put("battery_pct", batteryPct ?: JSONObject.NULL)
        return o.toString()
    }
}
