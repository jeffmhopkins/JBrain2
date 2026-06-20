package com.jbrain.dashboard

import java.net.URI

/** Decides which URLs the authenticated dashboard WebView may navigate to. Only
 * the dashboard's own origin is allowed; an off-origin link (an injected anchor, a
 * redirect, a tapped external URL) must never drive the member's signed-in WebView
 * somewhere else, where its session cookie could be misused. Pure (java.net only),
 * so it is unit-tested on the JVM. */
object NavigationPolicy {
    /** True iff [target] shares the dashboard base's origin (scheme + host + port). */
    fun sameOrigin(base: String, target: String): Boolean {
        val b = origin(base) ?: return false
        val t = origin(target) ?: return false
        return b == t
    }

    private fun origin(url: String): String? {
        return try {
            val u = URI(url)
            val scheme = u.scheme?.lowercase() ?: return null
            val host = u.host?.lowercase() ?: return null
            val port = if (u.port == -1) defaultPort(scheme) else u.port
            "$scheme://$host:$port"
        } catch (e: Exception) {
            null
        }
    }

    private fun defaultPort(scheme: String): Int = if (scheme == "http") 80 else 443
}
