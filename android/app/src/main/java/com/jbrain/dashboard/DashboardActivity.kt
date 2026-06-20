package com.jbrain.dashboard

import android.annotation.SuppressLint
import android.app.Activity
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebView

/** Hosts the member dashboard SPA (served at /dash) in a locked-down WebView.
 *
 * On launch the app reads the Keystore device key, mints a session cookie
 * natively (the key never reaches page JavaScript), injects it into the WebView
 * jar, and loads /dash. A missing/revoked key routes to pairing (M5c); a
 * transient failure shows a retry. No JavaScript bridge is registered, so page
 * script can never reach native APIs.
 */
class DashboardActivity : Activity() {
    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        web.settings.apply {
            javaScriptEnabled = true // the dashboard SPA is a React app
            domStorageEnabled = true // localStorage drives theme + font scale
            allowFileAccess = false // lockdown: no file:// reads
            allowContentAccess = false
        }
        setContentView(web)

        val launcher = SessionLauncher(KeystoreCredentialStore(this), SessionMinter())
        val base = BuildConfig.DASHBOARD_BASE
        // Mint off the main thread (network), then apply on the UI thread.
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
            // Placeholder states until the M5c pairing screen lands.
            LaunchDecision.NeedsPairing -> web.loadDataMessage("Pair this device from the JBrain app.")
            is LaunchDecision.Retry -> web.loadDataMessage("Couldn't reach the server — pull to retry.")
        }
    }

    private fun WebView.loadDataMessage(text: String) {
        loadData("<body style='font-family:sans-serif;padding:2rem'>$text</body>", "text/html", "utf-8")
    }
}
