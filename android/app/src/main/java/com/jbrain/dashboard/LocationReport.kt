package com.jbrain.dashboard

import org.json.JSONObject

/** An OwnTracks `_type:location` report. lat/lon/tst are required; the rest are
 * included only when known. `vel` (km/h) and `cog` (course, degrees) are standard
 * OwnTracks fields; `accel` (absolute linear-acceleration magnitude, m/s², gravity
 * removed and 0.2 s-filtered) is a JBrain360 extension the backend tolerates.
 * Encoding is pure (org.json), so it is JVM-unit-tested. */
data class LocationReport(
    val lat: Double,
    val lon: Double,
    val tst: Long, // capture instant, Unix epoch seconds
    val accuracyM: Int? = null,
    val velocityKmh: Int? = null,
    val courseDeg: Int? = null,
    val accelMps2: Double? = null,
    val batteryPct: Int? = null,
    val trackerId: String? = null,
) {
    fun toJson(): String {
        val o = JSONObject()
        o.put("_type", "location")
        o.put("lat", lat)
        o.put("lon", lon)
        o.put("tst", tst)
        accuracyM?.let { o.put("acc", it) }
        velocityKmh?.let { o.put("vel", it) }
        courseDeg?.let { o.put("cog", it) }
        accelMps2?.let { o.put("accel", it) }
        batteryPct?.let { o.put("batt", it) }
        trackerId?.let { o.put("tid", it) }
        return o.toString()
    }

    companion object {
        /** A JSON array of reports for a batched upload — the array shape
         * `/api/owntracks` accepts alongside a single object. */
        fun batchJson(reports: List<LocationReport>): String =
            reports.joinToString(separator = ",", prefix = "[", postfix = "]") { it.toJson() }

        /** Parse a `_type:location` line back into a report (for the offline queue);
         * null if it isn't a well-formed location, so a corrupt line can't wedge the
         * queue. */
        fun fromJson(json: String): LocationReport? = try {
            val o = JSONObject(json)
            if (o.optString("_type") != "location") {
                null
            } else {
                LocationReport(
                    lat = o.getDouble("lat"),
                    lon = o.getDouble("lon"),
                    tst = o.getLong("tst"),
                    accuracyM = if (o.has("acc")) o.getInt("acc") else null,
                    velocityKmh = if (o.has("vel")) o.getInt("vel") else null,
                    courseDeg = if (o.has("cog")) o.getInt("cog") else null,
                    accelMps2 = if (o.has("accel")) o.getDouble("accel") else null,
                    batteryPct = if (o.has("batt")) o.getInt("batt") else null,
                    trackerId = if (o.has("tid")) o.getString("tid") else null,
                )
            }
        } catch (e: Exception) {
            null
        }
    }
}
