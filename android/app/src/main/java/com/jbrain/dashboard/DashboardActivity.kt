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
 * interface is registered, so page script can never reach native APIs; the only
 * native->page channel is a one-way `evaluateJavascript` loopback that injects this
 * phone's own fixes for an instant self-pin (see registerLoopback).
 */
class DashboardActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var launcher: SessionLauncher

    // True once /dash is loaded, so a loopback fix is only injected into a live page.
    private var dashboardReady = false

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
                dashboardReady = true
                registerLoopback()
                // Paired + authenticated: begin sharing this phone's location.
                ensureLocationSharing()
            }
            LaunchDecision.NeedsPairing ->
                startActivityForResult(Intent(this, PairingActivity::class.java), REQ_PAIR)
            is LaunchDecision.Retry ->
                web.loadDataMessage("Couldn't reach the server — reopen to retry.")
        }
    }

    /** Forward this phone's own fixes from the location service into the page so the
     * self-pin moves instantly. Native -> page only (evaluateJavascript), so it adds
     * no JS -> native surface; the `&&` guard makes a fix before React mounts a no-op. */
    private fun registerLoopback() {
        if (!dashboardReady) return
        LocalFixBus.setListener { fix ->
            runOnUiThread {
                web.evaluateJavascript(
                    "window.__jbrainLocalFix && window.__jbrainLocalFix(${fix.toJson()})",
                    null,
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        registerLoopback() // re-arm after a background trip (cleared in onPause)
    }

    override fun onPause() {
        super.onPause()
        LocalFixBus.setListener(null) // don't push into a backgrounded page
    }

    override fun onDestroy() {
        super.onDestroy()
        LocalFixBus.setListener(null)
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
            // Request fine + coarse together so Android 12+ shows the precise/approximate
            // toggle; we only start once *fine* (precise) is actually granted.
            requestPermissions(
                arrayOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION,
                ),
                REQ_LOCATION,
            )
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
        // Start only on *precise* (fine): if the user picked "Approximate", don't
        // run on coarse — the map needs precise fixes.
        if (requestCode == REQ_LOCATION && hasFineLocation()) {
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
