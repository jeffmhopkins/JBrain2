package com.jbrain.dashboard

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.webkit.CookieManager
import androidx.core.content.getSystemService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject

/** Holds one authenticated SSE connection to the owner notifications stream and posts each
 * event as a local Android notification — the self-hosted delivery half (no FCM, no
 * Google). Foreground so the stream survives the app being backgrounded; it reconnects
 * with backoff. The owner's session cookie is read from the WebView's own cookie jar (set
 * when they signed in on the page), so the relay needs no separate auth. Content comes
 * straight through — it's the owner's server talking to the owner's device.
 */
class NotificationRelayService : Service() {
    @Volatile private var running = false
    private var worker: Thread? = null

    // A stream never idles out server-side (it sends keepalive comments), so no read
    // timeout; connection failures surface as exceptions and drive the reconnect loop.
    private val client =
        OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(true)
            .build()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!running) {
            running = true
            startInForeground()
            worker = Thread { loop() }.also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        worker?.interrupt()
        super.onDestroy()
    }

    /** Connect, read SSE frames, post a notification per `data:` event; reconnect with
     * capped backoff until the service is stopped. */
    private fun loop() {
        var backoffMs = 2_000L
        while (running) {
            val base = OwnerServerStore(this).base()
            val url = base?.let { OwnerConfig.ownerUrl(it).trimEnd('/') + STREAM_PATH }
            val cookie = url?.let { CookieManager.getInstance().getCookie(it) }
            if (url == null || cookie.isNullOrBlank()) {
                // Not configured or not signed in yet — wait and re-check.
                sleep(backoffMs)
                continue
            }
            try {
                val request =
                    Request.Builder()
                        .url(url)
                        .header("Accept", "text/event-stream")
                        .header("Cookie", cookie)
                        .build()
                client.newCall(request).execute().use { resp ->
                    if (resp.isSuccessful) {
                        backoffMs = 2_000L // connected → reset backoff
                        val source = resp.body?.source()
                        while (running && source != null && !source.exhausted()) {
                            val line = source.readUtf8Line() ?: break
                            if (line.startsWith("data:")) postFrom(line.substring(5).trim())
                        }
                    }
                }
            } catch (e: Exception) {
                // Dropped/failed — fall through to the backoff + reconnect.
            }
            if (running) {
                sleep(backoffMs)
                backoffMs = (backoffMs * 2).coerceAtMost(60_000L)
            }
        }
    }

    /** Render one stream event (a JSON `{kind,title,body,ref}`) as a notification whose tap
     * opens the owner app. A malformed frame is dropped. */
    private fun postFrom(json: String) {
        if (json.isEmpty()) return
        val obj =
            try {
                JSONObject(json)
            } catch (e: Exception) {
                return
            }
        val title = obj.optString("title").ifBlank { "JBrain" }
        val ref = obj.optString("ref")
        val tap =
            Intent(this, OwnerActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra("ref", ref)
        val pending =
            PendingIntent.getActivity(
                this,
                ref.hashCode(),
                tap,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        val note =
            Notification.Builder(this, CHANNEL_ALERTS)
                .setContentTitle(title)
                .setContentText(obj.optString("body"))
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setAutoCancel(true)
                .setContentIntent(pending)
                .build()
        getSystemService<NotificationManager>()?.notify(nextId.incrementAndGet(), note)
    }

    private fun startInForeground() {
        val mgr = getSystemService<NotificationManager>()
        mgr?.createNotificationChannel(
            NotificationChannel(CHANNEL_ALERTS, "Alerts", NotificationManager.IMPORTANCE_HIGH),
        )
        mgr?.createNotificationChannel(
            NotificationChannel(
                CHANNEL_FG,
                "Background connection",
                NotificationManager.IMPORTANCE_MIN,
            ),
        )
        val ongoing =
            Notification.Builder(this, CHANNEL_FG)
                .setContentTitle("JBrain")
                .setContentText("Listening for updates")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setOngoing(true)
                .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(FG_ID, ongoing, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(FG_ID, ongoing)
        }
    }

    private fun sleep(ms: Long) {
        try {
            Thread.sleep(ms)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }

    private companion object {
        const val STREAM_PATH = "/api/notifications/stream"
        const val CHANNEL_ALERTS = "owner_alerts"
        const val CHANNEL_FG = "owner_relay_fg"
        const val FG_ID = 42
        val nextId = AtomicInteger(1000)
    }
}
