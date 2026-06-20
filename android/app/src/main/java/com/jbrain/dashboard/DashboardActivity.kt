package com.jbrain.dashboard

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView

/** Hosts the member dashboard SPA (served at /dash) in a locked-down WebView.
 *
 * On launch the app reads the Keystore device key, mints a session cookie
 * natively (the key never reaches page JavaScript), injects it into the WebView
 * jar, and loads /dash. A missing/revoked key opens the pairing screen and
 * re-launches once paired; a transient failure shows a retry. No JavaScript
 * bridge is registered, so page script can never reach native APIs.
 */
class DashboardActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var launcher: SessionLauncher
    private val base = BuildConfig.DASHBOARD_BASE

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        web.settings.apply {
            javaScriptEnabled = true // the dashboard SPA is a React app
            domStorageEnabled = true // localStorage drives theme + font scale
            allowFileAccess = false // lockdown: no file:// reads
            allowContentAccess = false
            allowFileAccessFromFileURLs = false
            allowUniversalAccessFromFileURLs = false
            // The dashboard is https; never let an http subresource load into it.
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        }
        // Pin navigation to the dashboard origin; no JavaScript interface is added,
        // so page script can never reach native APIs.
        web.webViewClient = LockedWebViewClient(base)
        setContentView(web)
        launcher = SessionLauncher(KeystoreCredentialStore(this), SessionMinter())
        relaunch()
    }

    /** Read the key, mint off the main thread, then apply on the UI thread. */
    private fun relaunch() {
        Thread {
            val decision = launcher.launch(base)
            runOnUiThread { apply(decision) }
        }.start()
    }

    private fun apply(decision: LaunchDecision) {
        when (decision) {
            is LaunchDecision.Load -> {
                CookieManager.getInstance().apply {
                    setAcceptCookie(true)
                    setCookie(decision.url, decision.setCookie)
                }
                web.loadUrl(decision.url)
            }
            LaunchDecision.NeedsPairing ->
                startActivityForResult(Intent(this, PairingActivity::class.java), REQ_PAIR)
            is LaunchDecision.Retry ->
                web.loadDataMessage("Couldn't reach the server — reopen to retry.")
        }
    }

    @Deprecated("startActivityForResult is fine for this single flow on a plain Activity")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        // Paired successfully — try the launch again with the freshly stored key.
        if (requestCode == REQ_PAIR && resultCode == RESULT_OK) relaunch() else if (requestCode == REQ_PAIR) finish()
    }

    private fun WebView.loadDataMessage(text: String) {
        loadData("<body style='font-family:sans-serif;padding:2rem'>$text</body>", "text/html", "utf-8")
    }

    private companion object {
        const val REQ_PAIR = 1
    }
}
