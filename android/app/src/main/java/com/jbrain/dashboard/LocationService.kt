package com.jbrain.dashboard

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.net.ConnectivityManager
import android.net.Network
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
 * OwnTracks-compatible ingest the backend already runs. Precise fixes come from
 * GPS_PROVIDER (fine location). Moving: a fix at most every 30 s and only after
 * 25 m of travel (the distance filter keeps a parked phone quiet). Stationary: a
 * heartbeat forces one fresh fix at least every 15 min so the map never goes stale.
 * Every fix is queued to disk first and drained oldest-first, so a network lapse
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

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        store = KeystoreCredentialStore(this)
        uploader = LocationUploader(queue, publisher)
        registerConnectivity()
        startInForeground()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (store.deviceKey() == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        try {
            getSystemService<LocationManager>()?.requestLocationUpdates(
                LocationManager.GPS_PROVIDER,
                MIN_INTERVAL_MS,
                MIN_DISTANCE_M,
                this,
            )
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
        // Both the paired server and the key come from pairing; either gone → idle.
        val base = store.serverBase() ?: return
        val key = store.deviceKey() ?: return
        val report = LocationReport(
            lat = location.latitude,
            lon = location.longitude,
            tst = location.time / 1000,
            accuracyM = if (location.hasAccuracy()) location.accuracy.toInt() else null,
        )
        // Queue the fix, then drain the backlog oldest-first off the main thread.
        io.execute { handle(uploader.submit(base, key, report)) }
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
     * heartbeat window (the 25 m distance filter otherwise suppresses every update).
     * The fix lands in onLocationChanged, which re-arms the watchdog; we re-arm here
     * too so an indoor no-fix still retries next window. */
    @Suppress("DEPRECATION") // requestSingleUpdate; getCurrentLocation is API 30 > minSdk 26
    private fun requestSingleFix() {
        try {
            getSystemService<LocationManager>()?.requestSingleUpdate(
                LocationManager.GPS_PROVIDER,
                this,
                Looper.getMainLooper(),
            )
        } catch (e: SecurityException) {
            stopSelf()
        }
        armHeartbeat()
    }

    override fun onDestroy() {
        heartbeat.removeCallbacks(forceFix)
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
        const val MIN_INTERVAL_MS = 30_000L
        const val MIN_DISTANCE_M = 25f
        // Stationary heartbeat: at least one fix every 15 min even when parked.
        const val HEARTBEAT_MS = 15 * 60 * 1000L
    }
}
