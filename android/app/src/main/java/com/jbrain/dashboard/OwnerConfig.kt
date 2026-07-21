package com.jbrain.dashboard

/** Pure config for the owner app's WebView entry URL, unit-tested on the JVM (no Android
 * runtime needed). Mirrors [DashboardConfig] but points at the server root (the owner
 * SPA, index.html) rather than /dash. */
object OwnerConfig {
    /** The owner app's entry URL for a server base: normalise the base and keep the root.
     * Requires `https` — the owner session cookie must never travel in clear text. */
    fun ownerUrl(serverBase: String): String {
        val trimmed = serverBase.trim().trimEnd('/')
        require(trimmed.startsWith("https://")) { "server base must be https" }
        return "$trimmed/"
    }

    /** Whether [raw] is a usable https base — the setup screen's validation, kept here so
     * it's covered by the JVM tests. */
    fun isValidBase(raw: String): Boolean {
        val trimmed = raw.trim().trimEnd('/')
        return trimmed.startsWith("https://") && trimmed.length > "https://".length
    }
}
