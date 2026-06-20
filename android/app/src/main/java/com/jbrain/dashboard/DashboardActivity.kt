package com.jbrain.dashboard

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import androidx.core.content.ContextCompat

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
        // No JavaScript interface is registered, so page script can never reach
        // native APIs. The navigation-origin lock is pinned per load (the paired
        // server isn't known until launch).
        setContentView(web)
        launcher = SessionLauncher(KeystoreCredentialStore(this), SessionMinter())
        relaunch()
    }

    /** Read the paired server + key, mint off the main thread, apply on the UI thread. */
    private fun relaunch() {
        Thread {
            val decision = launcher.launch()
            runOnUiThread { apply(decision) }
        }.start()
    }

    private fun apply(decision: LaunchDecision) {
        when (decision) {
            is LaunchDecision.Load -> {
                // Pin navigation to the paired dashboard's own origin before loading.
                web.webViewClient = LockedWebViewClient(decision.url)
                CookieManager.getInstance().apply {
                    setAcceptCookie(true)
                    setCookie(decision.url, decision.setCookie)
                }
                web.loadUrl(decision.url)
                // Paired + authenticated: begin sharing this phone's location.
                ensureLocationSharing()
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

    /** Start the location-publishing service once we have foreground-location
     * permission; otherwise request it and start on grant. Background-location +
     * the doze/OEM hardening are a later pass. */
    private fun ensureLocationSharing() {
        if (hasFineLocation()) {
            startService(Intent(this, LocationService::class.java))
        } else {
            requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION), REQ_LOCATION)
        }
    }

    private fun hasFineLocation(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_LOCATION && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            startService(Intent(this, LocationService::class.java))
        }
    }

    private fun WebView.loadDataMessage(text: String) {
        loadData("<body style='font-family:sans-serif;padding:2rem'>$text</body>", "text/html", "utf-8")
    }

    private companion object {
        const val REQ_PAIR = 1
        const val REQ_LOCATION = 2
    }
}
