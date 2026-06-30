package com.jbrain.dashboard

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** The single revival point: every periodic watchdog alarm and the boot broadcast
 * lands here. It restarts the location service when it should be running and re-arms
 * the next watchdog tick, so the loop is self-perpetuating — one kick (boot, app
 * launch, pairing) keeps it ticking until the device is unpaired.
 *
 * Re-arming is gated on being paired, not on the service actually starting: a missing
 * location grant or a closed background-start window must not stop the watchdog, or it
 * could never recover once the grant returns. Only an unpaired device (no key) lets it
 * lapse — pairing re-arms it. */
class WatchdogReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        LocationServiceControl.startIfEligible(context)
        // Keep the watchdog alive while paired so a later permission grant or a
        // doze-killed service is recovered on the next tick.
        if (KeystoreCredentialStore(context).deviceKey() != null) {
            WatchdogScheduler.armPeriodic(context)
        }
    }
}
