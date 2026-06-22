package com.jbrain.dashboard

/** An in-process hand-off of this phone's own fixes from the location service to the
 * dashboard WebView, so the map shows self-movement the instant a fix lands — the
 * network upload is batched (up to ~30 s), so without this the self-pin lags while
 * driving.
 *
 * One listener: the visible DashboardActivity, which forwards each fix into the page
 * via `evaluateJavascript` (native -> page only, so this adds no JS -> native surface
 * and keeps the no-bridge lockdown). A stale fix from a backgrounded run is harmless:
 * the activity clears its listener when not in the foreground. */
object LocalFixBus {
    @Volatile
    private var listener: ((LocalFix) -> Unit)? = null

    fun setListener(l: ((LocalFix) -> Unit)?) {
        listener = l
    }

    fun publish(fix: LocalFix) {
        listener?.invoke(fix)
    }
}
