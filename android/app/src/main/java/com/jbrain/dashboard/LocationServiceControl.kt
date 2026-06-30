package com.jbrain.dashboard

import android.Manifest
import android.app.AlarmManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.PowerManager
import androidx.core.content.ContextCompat
import androidx.core.content.getSystemService

/** One way to (re)start the location service, shared by the foreground launch
 * (`DashboardActivity`), boot, and the watchdog alarm. Centralised so every caller
 * starts it the same way: a background start MUST go through `startForegroundService`
 * on O+, and the call is wrapped so a denied background-start (no FGS exemption on
 * Android 12+) is swallowed — the next launch or battery-allowlisted tick recovers
 * rather than crashing the receiver. */
object LocationServiceControl {
    /** Start the service iff it should run (paired + precise location). Returns true
     * when a start was issued. Reads the two preconditions itself so every revival
     * path makes the identical `ServiceRunPolicy` decision. */
    fun startIfEligible(context: Context): Boolean {
        val paired = KeystoreCredentialStore(context).deviceKey() != null
        if (!ServiceRunPolicy.shouldRun(paired, hasFineLocation(context))) return false
        val intent = Intent(context, LocationService::class.java)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        } catch (e: Exception) {
            // ForegroundServiceStartNotAllowedException (S+) when the background-start
            // window is closed and the app isn't battery-allowlisted: drop it, the
            // next eligible tick/launch recovers. Other start failures are equally
            // non-fatal to the receiver.
            return false
        }
        return true
    }

    fun hasFineLocation(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    /** True once the app is exempt from battery optimization — being on the allowlist
     * both stops doze suspending the service and is itself a documented
     * foreground-service background-start exemption. */
    fun isBatteryUnrestricted(context: Context): Boolean {
        val pm = context.getSystemService<PowerManager>() ?: return true
        return pm.isIgnoringBatteryOptimizations(context.packageName)
    }

    /** Whether exact alarms are available for the doze-piercing watchdog (always on
     * pre-S; user-revocable on S+). */
    fun canScheduleExactAlarms(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
        val am = context.getSystemService<AlarmManager>() ?: return false
        return am.canScheduleExactAlarms()
    }
}
