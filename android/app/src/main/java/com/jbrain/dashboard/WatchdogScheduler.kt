package com.jbrain.dashboard

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import androidx.core.content.getSystemService

/** Arms the self-perpetuating revival alarm that brings `LocationService` back when an
 * aggressive OEM (doze, a battery killer, a swipe-from-recents) has stopped it. Each
 * fire lands in [WatchdogReceiver], which restarts the service if it should be running
 * and re-arms the next tick — so a single kick (boot, pairing, app launch) keeps the
 * watchdog ticking indefinitely.
 *
 * `setExactAndAllowWhileIdle` is chosen deliberately: it pierces doze (a plain alarm
 * is held to the maintenance window) AND an exact alarm firing grants a short
 * foreground-service-start allowance on Android 12+, which an inexact one does not —
 * so the receiver can actually (re)start the location FGS from the background. When the
 * OS withholds exact-alarm permission we fall back to the inexact allow-while-idle
 * variant: degraded cadence, but the boot/launch/battery-allowlist paths still cover
 * the common kills. */
object WatchdogScheduler {
    // Match the stationary heartbeat: a revived service that was killed mid-park still
    // reports within one heartbeat of coming back.
    const val PERIOD_MS = 15 * 60 * 1000L
    private const val REQUEST_CODE = 1001
    const val ACTION_WATCHDOG = "com.jbrain.dashboard.action.WATCHDOG"

    /** Arm the next periodic revival tick (replaces any pending one). */
    fun armPeriodic(context: Context) = armAt(context, PERIOD_MS)

    /** Arm a near-term restart — used after the task is swiped from recents so
     * tracking resumes in seconds rather than waiting a full period. */
    fun armSoon(context: Context, delayMs: Long = 2_000L) = armAt(context, delayMs)

    private fun armAt(context: Context, delayMs: Long) {
        val am = context.getSystemService<AlarmManager>() ?: return
        val triggerAt = SystemClock.elapsedRealtime() + delayMs
        val pi = pendingIntent(context)
        if (canExact(am)) {
            am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pi)
        } else {
            am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, triggerAt, pi)
        }
    }

    private fun canExact(am: AlarmManager): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms()

    private fun pendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, WatchdogReceiver::class.java).setAction(ACTION_WATCHDOG)
        // Explicit component + FLAG_IMMUTABLE: the OS requires immutable PendingIntents
        // on S+, and an explicit target needs no exported receiver/intent-filter.
        return PendingIntent.getBroadcast(
            context,
            REQUEST_CODE,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }
}
