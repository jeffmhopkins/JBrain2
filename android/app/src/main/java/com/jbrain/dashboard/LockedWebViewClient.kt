package com.jbrain.dashboard

import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/** Pins the WebView to the dashboard origin: any navigation off it (an external
 * link, a redirect to another site) is refused rather than loaded, so the member's
 * authenticated session never follows a link off the dashboard. The decision lives
 * in the JVM-tested [NavigationPolicy]. */
class LockedWebViewClient(private val base: String) : WebViewClient() {
    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        // Return true = "we handled it" (by doing nothing) → the load is blocked.
        return !NavigationPolicy.sameOrigin(base, request.url.toString())
    }
}
