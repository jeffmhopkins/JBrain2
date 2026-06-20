package com.jbrain.dashboard

/** Pure config helpers, unit-tested on the JVM (no Android runtime needed). */
object DashboardConfig {
    /** The dashboard entry URL for a server base: normalises the base and appends
     * the `/dash` path. Requires `https` — the session cookie and the native key
     * exchange must never travel in clear text. */
    fun dashboardUrl(serverBase: String): String {
        val trimmed = serverBase.trim().trimEnd('/')
        require(trimmed.startsWith("https://")) { "server base must be https" }
        return "$trimmed/dash"
    }
}
