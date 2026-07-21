package com.jbrain.dashboard

import android.content.Intent
import android.net.Uri
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/** Keeps the owner's signed-in WebView on its own origin: an off-origin link (a research
 * citation, an external site the assistant surfaces) opens in the system browser rather
 * than navigating the authenticated WebView there, where its session cookie could follow.
 * Same-origin loads proceed normally. The same-origin decision lives in the JVM-tested
 * [NavigationPolicy].
 *
 * [onMainFrameError] fires only when the top document itself fails to load (an offline
 * cold-start), never for a failed subresource, so a partly-rendered page isn't replaced. */
class OwnerWebViewClient(
    private val base: String,
    private val onMainFrameError: (() -> Unit)? = null,
) : WebViewClient() {
    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        val url = request.url.toString()
        if (NavigationPolicy.sameOrigin(base, url)) return false // let the WebView load it
        // Off-origin: hand it to the system browser; never navigate the session WebView there.
        return try {
            view.context.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            true
        } catch (e: Exception) {
            true // no browser to handle it — still refuse to load it in-session
        }
    }

    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        if (request.isForMainFrame) onMainFrameError?.invoke()
    }
}
