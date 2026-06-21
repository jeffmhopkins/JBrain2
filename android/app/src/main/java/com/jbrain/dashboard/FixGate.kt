package com.jbrain.dashboard

/** The device-side accuracy gate: only reasonably-accurate fixes are stored, so
 * jittery indoor GPS never enters the queue (the server geofence gate and the map
 * render apply the same idea at 100 m; the device is stricter at 50 m so the trail
 * is built from good fixes only). Pure, so it is JVM-unit-tested. */
object FixGate {
    const val MAX_ACCURACY_M = 50

    /** Keep a fix whose accuracy radius is within the gate; a null/unknown accuracy
     * is kept rather than assumed bad. */
    fun accept(accuracyM: Int?): Boolean = accuracyM == null || accuracyM <= MAX_ACCURACY_M
}
