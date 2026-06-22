package com.jbrain.dashboard

import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import kotlin.math.sqrt

/** Tracks the phone's absolute linear-acceleration magnitude, low-pass filtered to
 * a 0.2 s time constant.
 *
 * Uses `TYPE_LINEAR_ACCELERATION` (the OS already removes gravity via sensor
 * fusion), so the value reads ~0 at rest and rises with real acceleration/braking.
 * The three axes are reduced to a single direction-independent magnitude, then
 * smoothed by [LowPassFilter] so a single jolt does not spike the reported value.
 * The latest filtered magnitude is sampled at each GPS fix; the sensor stream runs
 * independently while the service holds it open. A device without the sensor simply
 * reports null (the field is omitted from the fix). */
class AccelerationMeter(private val sensors: SensorManager?) : SensorEventListener {
    private val filter = LowPassFilter(TAU_SECONDS)
    private val sensor: Sensor? = sensors?.getDefaultSensor(Sensor.TYPE_LINEAR_ACCELERATION)

    /** Begin sampling. No-op when the device lacks a linear-acceleration sensor. */
    fun start() {
        val s = sensor ?: return
        // UI-rate (~60 ms) gives several samples per 0.2 s time constant — enough to
        // filter cleanly without the battery cost of the game/fastest rates.
        sensors?.registerListener(this, s, SensorManager.SENSOR_DELAY_UI)
    }

    fun stop() {
        sensors?.unregisterListener(this)
    }

    /** Latest filtered absolute acceleration in m/s², or null before the first
     * sample (or when the device has no linear-acceleration sensor). */
    fun latest(): Double? = filter.latest()

    override fun onSensorChanged(event: SensorEvent) {
        val x = event.values[0]
        val y = event.values[1]
        val z = event.values[2]
        filter.update(sqrt((x * x + y * y + z * z).toDouble()), event.timestamp)
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    private companion object {
        const val TAU_SECONDS = 0.2
    }
}
