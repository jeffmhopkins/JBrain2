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
import android.os.Build
import android.os.IBinder
import androidx.core.content.getSystemService

/** A foreground service that publishes the phone's location to `/api/owntracks`.
 *
 * Minimal by design (framework LocationManager — no Play Services), posting to the
 * OwnTracks-compatible ingest the backend already runs. It reads the device key
 * from the Keystore store, stops cleanly if unpaired, and clears the key + stops if
 * the server reports it revoked. Reliability across aggressive OEMs (doze, battery
 * killing) is a deliberate later hardening pass.
 */
class LocationService : Service(), LocationListener {
    private val publisher = LocationPublisher()
    private lateinit var store: CredentialStore

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        store = KeystoreCredentialStore(this)
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
        } catch (e: SecurityException) {
            stopSelf() // the location permission isn't granted
        }
        return START_STICKY
    }

    override fun onLocationChanged(location: Location) {
        // Both the paired server and the key come from pairing; either gone → idle.
        val base = store.serverBase() ?: return
        val key = store.deviceKey() ?: return
        val report = LocationReport(
            lat = location.latitude,
            lon = location.longitude,
            tst = location.time / 1000,
            accuracyM = if (location.hasAccuracy()) location.accuracy.toInt() else null,
        )
        Thread {
            if (publisher.publish(base, key, report) is PublishOutcome.Unauthorized) {
                store.clear() // the key was revoked — stop sharing until re-paired
                stopSelf()
            }
        }.start()
    }

    override fun onDestroy() {
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
    }
}
