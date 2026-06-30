package com.jbrain.dashboard

import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/** Pins the WebView to the dashboard origin: any navigation off it (an external
 * link, a redirect to another site) is refused rather than loaded, so the member's
 * authenticated session never follows a link off the dashboard. The decision lives
 * in the JVM-tested [NavigationPolicy].
 *
 * [onMainFrameError] fires only when the top document itself fails to load (e.g. an
 * offline cold-start with no cached page) — never for a failed subresource like a map
 * tile out of coverage, so a partially-cached map still renders instead of being
 * replaced by the fallback. */
class LockedWebViewClient(
    private val base: String,
    private val onMainFrameError: (() -> Unit)? = null,
) : WebViewClient() {
    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        // Return true = "we handled it" (by doing nothing) → the load is blocked.
        return !NavigationPolicy.sameOrigin(base, request.url.toString())
    }

    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        if (request.isForMainFrame) onMainFrameError?.invoke()
    }
}
