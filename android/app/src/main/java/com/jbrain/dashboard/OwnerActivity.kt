package com.jbrain.dashboard

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.window.OnBackInvokedDispatcher

/** Hosts the OWNER app (the SPA at the server root) in a WebView, so the system back
 * gesture is a native callback we control rather than the browser's — the reliability the
 * PWA can't guarantee on Android's gesture/predictive back. Back climbs the page's own
 * layer stack through its `window.__jbrainBack()` bridge and, when nothing is open,
 * BACKGROUNDS the app (moveTaskToBack) instead of exiting.
 *
 * The owner signs in through the web page itself (an owner key), so — unlike the member
 * dashboard — there is no native key/cookie mint and no native location service (that
 * uploads with the paired device key the owner doesn't have; the owner's own location
 * rides the web session's browser geolocation). This host only needs the server URL
 * (OwnerSetupActivity). No JavaScript interface is registered: the only native->page
 * channel is one-way (evaluateJavascript), so page script can never reach native APIs.
 */
class OwnerActivity : Activity() {
    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        web.settings.apply {
            javaScriptEnabled = true // the owner app is a React SPA
            domStorageEnabled = true // localStorage drives theme + font scale + drafts
            allowFileAccess = false // lockdown: no file:// reads
            allowContentAccess = false
            allowFileAccessFromFileURLs = false
            allowUniversalAccessFromFileURLs = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            // Mark the native host so the page skips its History API back trap and lets this
            // activity drive back through window.__jbrainBack (see useBackGesture.ts).
            userAgentString = "$userAgentString JBrainOwner/1"
        }
        CookieManager.getInstance().setAcceptCookie(true)
        setContentView(web)

        // Route the system back button to the page's layer stack: predictive back on 33+,
        // the legacy onBackPressed below it. Both background instead of exiting when the
        // page reports nothing left to close.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            onBackInvokedDispatcher.registerOnBackInvokedCallback(
                OnBackInvokedDispatcher.PRIORITY_DEFAULT,
            ) { handleBack() }
        }

        val base = OwnerServerStore(this).base()
        if (base == null) {
            startActivityForResult(Intent(this, OwnerSetupActivity::class.java), REQ_SETUP)
        } else {
            load(base)
        }
    }

    private fun load(base: String) {
        val url = OwnerConfig.ownerUrl(base)
        web.webViewClient = OwnerWebViewClient(url) { onLoadFailed() }
        web.loadUrl(url)
    }

    private fun onLoadFailed() {
        web.loadData(
            "<body style='font-family:sans-serif;padding:2rem'>Couldn't reach the server — reopen to retry.</body>",
            "text/html",
            "utf-8",
        )
    }

    /** Back: ask the page to close its topmost layer; if it had none (or hasn't loaded),
     * background the app instead of exiting. evaluateJavascript is async, so the decision
     * lands in its callback on the UI thread. */
    private fun handleBack() {
        web.evaluateJavascript("(window.__jbrainBack && window.__jbrainBack()) === true") { result ->
            if (result != "true") moveTaskToBack(true)
        }
    }

    @Deprecated("Legacy back path for API < 33; 33+ routes to the OnBackInvokedCallback.")
    override fun onBackPressed() {
        handleBack()
    }

    @Deprecated("startActivityForResult is fine for this single flow on a plain Activity")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_SETUP && resultCode == RESULT_OK) {
            val base = OwnerServerStore(this).base()
            if (base != null) load(base) else finish()
        } else if (requestCode == REQ_SETUP) {
            finish()
        }
    }

    private companion object {
        const val REQ_SETUP = 1
    }
}
