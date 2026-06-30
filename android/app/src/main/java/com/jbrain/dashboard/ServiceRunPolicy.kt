package com.jbrain.dashboard

/** Should the location-publishing service be running right now? It must be, exactly
 * when the device is paired (a device key is held) AND precise location is granted —
 * the same two preconditions `LocationService` checks at start. Centralised here as a
 * pure predicate so every revival path (boot, the watchdog alarm, task-removal) makes
 * the identical call before starting the foreground service, and so it is
 * JVM-unit-tested without Android. */
object ServiceRunPolicy {
    fun shouldRun(paired: Boolean, hasFineLocation: Boolean): Boolean =
        paired && hasFineLocation
}
