package com.jbrain.dashboard

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.SensorManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.net.ConnectivityManager
import android.net.Network
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.content.getSystemService
import java.io.File
import java.util.concurrent.Executors

/** A foreground service that publishes the phone's location to `/api/owntracks`.
 *
 * Minimal by design (framework LocationManager — no Play Services), posting to the
 * OwnTracks-compatible ingest the backend already runs. Precise fixes come from the
 * platform FUSED provider where the OS offers one (API 31+, on a Pixel that is
 * Google's own sensor fusion — smoothed, no Play Services), falling back to
 * GPS_PROVIDER. A `SamplingPolicy` adapts the cadence to motion: while moving, a
 * fix every ~5 s after ~8 m of travel (a dense trail); while parked it relaxes and a
 * heartbeat forces one fresh fix every 15 min so the map never goes stale. A device
 * accuracy gate (`FixGate`, 50 m) drops jittery fixes before they are queued. Every
 * kept fix is queued to disk first and drained oldest-first, so a network lapse
 * backfills (in order, with real capture times) on the next fix or when connectivity
 * returns rather than dropping points. It reads the device key from the Keystore
 * store, stops cleanly if unpaired, and clears the key + stops if the server reports
 * it revoked. Reliability across aggressive OEMs (doze, battery killing) is a
 * deliberate later hardening pass.
 */
class LocationService : Service(), LocationListener {
    private val publisher = LocationPublisher()
    private lateinit var store: CredentialStore
    private lateinit var uploader: LocationUploader
    private val queue: FixQueue by lazy { FileFixQueue(File(filesDir, "fixes.ndjson")) }
    // One worker thread owns all queue + network I/O, so drains never overlap or
    // touch the main thread.
    private val io = Executors.newSingleThreadExecutor()
    private val heartbeat = Handler(Looper.getMainLooper())
    private val forceFix = Runnable { requestSingleFix() }
    private var connectivity: ConnectivityManager.NetworkCallback? = null
    // Motion-adaptive cadence; `current` is the params last requested, so a state
    // change (moving <-> stationary) re-requests updates and nothing else does.
    private val policy = SamplingPolicy()
    private var current: RequestParams = policy.movingParams
    // Continuous accelerometer stream; its latest 0.2 s-filtered magnitude is sampled
    // onto each fix. Held open for the service's life (started/stopped with it).
    private lateinit var accel: AccelerationMeter
    // When the queue was last flushed (io thread only) — the batched-upload cadence.
    private var lastFlushMs = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        store = KeystoreCredentialStore(this)
        uploader = LocationUploader(queue, publisher)
        accel = AccelerationMeter(getSystemService<SensorManager>())
        accel.start()
        registerConnectivity()
        startInForeground()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (store.deviceKey() == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            current = policy.params()
            requestUpdates(current)
            // Guarantee a fix within the heartbeat even if the phone is parked from
            // the start (movement updates would otherwise never fire).
            armHeartbeat()
        } catch (e: SecurityException) {
            stopSelf() // precise location isn't granted
        }
        return START_STICKY
    }

    override fun onLocationChanged(location: Location) {
        // A fix arrived (movement update or forced heartbeat) — reset the 15-min
        // watchdog so it only fires after a genuine gap with no fixes.
        armHeartbeat()
        val accuracyM = if (location.hasAccuracy()) location.accuracy.toInt() else null
        // Drop jittery wide-radius fixes before they reach the trail (the star-burst
        // fix); the heartbeat is still re-armed above so a bad-fix streak retries.
        if (!FixGate.accept(accuracyM)) return
        // Adapt the cadence to motion: a transition (moving <-> stationary) returns
        // new params, so re-request updates; otherwise leave the provider as-is.
        val next = policy.onFix(location.latitude, location.longitude, location.time)
        if (next != current) {
            current = next
            requestUpdates(next)
        }
        // Both the paired server and the key come from pairing; either gone → idle.
        val base = store.serverBase() ?: return
        val key = store.deviceKey() ?: return
        val report = LocationReport(
            lat = location.latitude,
            lon = location.longitude,
            tst = location.time / 1000,
            accuracyM = accuracyM,
            // m/s -> km/h (OwnTracks `vel`); bearing is course-over-ground in degrees.
            velocityKmh = if (location.hasSpeed()) Math.round(location.speed * 3.6).toInt() else null,
            courseDeg = if (location.hasBearing()) Math.round(location.bearing).toInt() else null,
            accelMps2 = accel.latest(),
            batteryPct = batteryPct(),
        )
        // Persist the fix, then flush the backlog as a batch on a cadence (every N
        // points or T seconds) so a dense moving stream costs few requests instead
        // of one POST per fix. All queue + network I/O stays on the single io thread.
        io.execute {
            uploader.enqueue(report)
            val now = System.currentTimeMillis()
            if (uploader.pending() >= FLUSH_POINTS || now - lastFlushMs >= FLUSH_INTERVAL_MS) {
                lastFlushMs = now
                handle(uploader.flush(base, key))
            }
        }
    }

    /** (Re-)subscribe to location updates at the given cadence from the best
     * provider the OS offers — the platform fused provider where present (API 31+),
     * else GPS. Re-requesting replaces the prior subscription. */
    private fun requestUpdates(params: RequestParams) {
        val lm = getSystemService<LocationManager>() ?: return
        try {
            lm.removeUpdates(this)
            lm.requestLocationUpdates(provider(lm), params.intervalMs, params.displacementM, this)
        } catch (e: SecurityException) {
            stopSelf() // precise location was revoked mid-stream
        }
    }

    /** The platform FUSED provider (API 31+, when the device supplies one) gives
     * smoother sensor-fused fixes with no Play Services; otherwise raw GPS. */
    private fun provider(lm: LocationManager): String =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S &&
            lm.allProviders.contains(LocationManager.FUSED_PROVIDER)
        ) {
            LocationManager.FUSED_PROVIDER
        } else {
            LocationManager.GPS_PROVIDER
        }

    /** Act on a flush outcome: a revoked key clears the credentials + queue and stops. */
    private fun handle(result: FlushResult) {
        if (result is FlushResult.Unauthorized) {
            store.clear()
            queue.clear()
            stopSelf()
        }
    }

    /** Flush the backlog whenever a network becomes available, so a lapse backfills
     * immediately instead of waiting for the next fix. */
    private fun registerConnectivity() {
        val cm = getSystemService<ConnectivityManager>() ?: return
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                io.execute {
                    val base = store.serverBase() ?: return@execute
                    val key = store.deviceKey() ?: return@execute
                    lastFlushMs = System.currentTimeMillis() // keep the fix-cadence in sync
                    handle(uploader.flush(base, key))
                }
            }
        }
        connectivity = cb
        cm.registerDefaultNetworkCallback(cb)
    }

    /** Re-arm the stationary watchdog: if no fix lands within HEARTBEAT_MS, force one. */
    private fun armHeartbeat() {
        heartbeat.removeCallbacks(forceFix)
        heartbeat.postDelayed(forceFix, HEARTBEAT_MS)
    }

    /** Force one fresh precise fix so a parked phone still reports within the
     * heartbeat window (the displacement filter otherwise suppresses every update).
     * The fix lands in onLocationChanged, which re-arms the watchdog; we re-arm here
     * too so an indoor no-fix still retries next window. */
    @Suppress("DEPRECATION") // requestSingleUpdate; getCurrentLocation is API 30 > minSdk 26
    private fun requestSingleFix() {
        try {
            getSystemService<LocationManager>()?.let {
                it.requestSingleUpdate(provider(it), this, Looper.getMainLooper())
            }
        } catch (e: SecurityException) {
            stopSelf()
        }
        armHeartbeat()
    }

    /** The current charge level as a whole percent (0–100), or null when the
     * platform can't report it. Read fresh per fix so the trail carries battery. */
    private fun batteryPct(): Int? {
        val bm = getSystemService<BatteryManager>() ?: return null
        val pct = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        return if (pct in 0..100) pct else null
    }

    override fun onDestroy() {
        heartbeat.removeCallbacks(forceFix)
        accel.stop()
        connectivity?.let { getSystemService<ConnectivityManager>()?.unregisterNetworkCallback(it) }
        io.shutdown()
        try {
            getSystemService<LocationManager>()?.removeUpdates(this)
        } catch (e: SecurityException) {
            // already torn down
        }
        super.onDestroy()
    }

    private fun startInForeground() {
        getSystemService<NotificationManager>()?.createNotificationChannel(
            NotificationChannel(CHANNEL, "Location sharing", NotificationManager.IMPORTANCE_LOW),
        )
        val notification: Notification = Notification.Builder(this, CHANNEL)
            .setContentTitle("JBrain360")
            .setContentText("Sharing your location with your family")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION)
        } else {
            startForeground(NOTIF_ID, notification)
        }
    }

    private companion object {
        const val CHANNEL = "location"
        const val NOTIF_ID = 1
        // Stationary heartbeat: at least one fix every 15 min even when parked.
        // (Moving/stationary cadence lives in SamplingPolicy.)
        const val HEARTBEAT_MS = 15 * 60 * 1000L
        // Batched upload cadence: flush once ~12 points accumulate (≈1 min moving at
        // 5 s) or at least every 30 s, so a sparse/stationary fix still uploads
        // promptly while a dense stream is coalesced into few requests.
        const val FLUSH_POINTS = 12
        const val FLUSH_INTERVAL_MS = 30_000L
    }
}
