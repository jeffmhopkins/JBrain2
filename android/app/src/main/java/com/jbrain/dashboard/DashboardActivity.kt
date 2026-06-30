package com.jbrain.dashboard

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import androidx.core.content.ContextCompat
import androidx.core.content.getSystemService

/** Hosts the member dashboard SPA (served at /dash) in a locked-down WebView.
 *
 * On launch the app reads the Keystore device key, mints a session cookie
 * natively (the key never reaches page JavaScript), injects it into the WebView
 * jar, and loads /dash. A missing/revoked key opens the pairing screen and
 * re-launches once paired; offline, it falls back to the cached dashboard (cached
 * tiles + the live self-pin) and keeps capturing fixes, else shows a retry. No JavaScript
 * interface is registered, so page script can never reach native APIs; the only
 * native->page channel is a one-way `evaluateJavascript` loopback that injects this
 * phone's own fixes for an instant self-pin (see registerLoopback).
 */
class DashboardActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var launcher: SessionLauncher
    private lateinit var store: CredentialStore

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
        store = KeystoreCredentialStore(this)
        launcher = SessionLauncher(store, SessionMinter())
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
                CookieManager.getInstance().apply {
                    setAcceptCookie(true)
                    setCookie(decision.url, decision.setCookie)
                }
                loadDashboard(decision.url, useCache = false)
                // Paired + authenticated: begin sharing this phone's location.
                ensureLocationSharing()
            }
            LaunchDecision.NeedsPairing ->
                startActivityForResult(Intent(this, PairingActivity::class.java), REQ_PAIR)
            is LaunchDecision.Retry -> {
                // Paired, but the session mint failed (usually offline). Capture anyway —
                // fixes queue on-device and backfill when signal returns — and, offline,
                // render the cached dashboard (cached tiles + the live self-pin) instead
                // of an error wall. The dashboard's gate tolerates the failed session
                // probe while offline and shows its "caching fixes" badge.
                ensureLocationSharing()
                val base = store.serverBase()
                if (base != null && !isOnline()) {
                    loadDashboard(DashboardConfig.dashboardUrl(base), useCache = true)
                } else {
                    web.loadDataMessage("Couldn't reach the server — reopen to retry.")
                }
            }
        }
    }

    /** Load the dashboard at [url] with navigation pinned to its origin. With
     * [useCache] (the offline path) the WebView serves from its HTTP cache, so a
     * dashboard and the map tiles visited before still render out of signal; a
     * main-document cache miss falls back to the offline message rather than a broken
     * page. Online loads always go to the network so the SPA is never stale. */
    private fun loadDashboard(url: String, useCache: Boolean) {
        web.webViewClient = LockedWebViewClient(url) { onDashboardUnavailable() }
        web.settings.cacheMode =
            if (useCache) WebSettings.LOAD_CACHE_ELSE_NETWORK else WebSettings.LOAD_DEFAULT
        web.loadUrl(url)
        dashboardReady = true
        registerLoopback()
    }

    /** The cached dashboard couldn't load (nothing cached yet) — show a plain offline
     * note instead of the browser's broken-page chrome. Capture is already running. */
    private fun onDashboardUnavailable() {
        dashboardReady = false
        web.loadDataMessage("Offline — no cached map yet. Reconnect once to load it; your location is still being recorded.")
    }

    /** Whether the device currently has a validated internet route — drives the
     * WebView cache mode and the offline-dashboard fallback. */
    private fun isOnline(): Boolean {
        val cm = getSystemService<ConnectivityManager>() ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork ?: return false) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
            caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
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
     * permission; otherwise request it and start on grant. Once running, walk the
     * background-survival grants (notifications, "Allow all the time", battery-opt
     * exemption) so tracking keeps reporting after the app is backgrounded. */
    private fun ensureLocationSharing() {
        if (hasFineLocation()) {
            startSharing()
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

    /** Foreground location is granted: start the service, arm the revival watchdog, and
     * begin the one-time walk through the remaining background-survival grants. */
    private fun startSharing() {
        startService(Intent(this, LocationService::class.java))
        WatchdogScheduler.armPeriodic(this)
        requestNotificationsThenBackground()
    }

    /** Android 13+ needs a runtime grant for the foreground-service notification; ask,
     * then move on to background location either way (the service runs regardless). */
    private fun requestNotificationsThenBackground() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF)
        } else {
            requestBackgroundLocation()
        }
    }

    /** "Allow all the time" — required for the boot/watchdog restart paths to read
     * location while backgrounded. A separate prompt by OS rule (can't be bundled with
     * the foreground grant); on Android 11+ it routes the user to settings. */
    private fun requestBackgroundLocation() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_BACKGROUND_LOCATION) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), REQ_BACKGROUND)
        } else {
            promptBatteryExemption()
        }
    }

    /** Ask to be exempted from battery optimization: it keeps doze from suspending the
     * service AND is itself a foreground-service background-start exemption, so the
     * watchdog can revive the service. Best-effort — a declined prompt just leaves the
     * app subject to OEM throttling. */
    private fun promptBatteryExemption() {
        if (LocationServiceControl.isBatteryUnrestricted(this)) return
        // Ask once: the system dialog can't be permanently dismissed like a runtime
        // permission, so without this flag a declined prompt would re-open every launch.
        val prefs = getSharedPreferences("survival", MODE_PRIVATE)
        if (prefs.getBoolean("battery_asked", false)) return
        prefs.edit().putBoolean("battery_asked", true).apply()
        try {
            startActivity(
                Intent(
                    Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:$packageName"),
                ),
            )
        } catch (e: Exception) {
            // Some OEMs don't expose the direct intent; the user can still allowlist
            // the app manually from system battery settings.
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
        when (requestCode) {
            // Start only on *precise* (fine): if the user picked "Approximate", don't
            // run on coarse — the map needs precise fixes.
            REQ_LOCATION -> if (hasFineLocation()) startSharing()
            // Each later grant just advances the walk; the service is already running.
            REQ_NOTIF -> requestBackgroundLocation()
            REQ_BACKGROUND -> promptBatteryExemption()
        }
    }

    private fun WebView.loadDataMessage(text: String) {
        loadData("<body style='font-family:sans-serif;padding:2rem'>$text</body>", "text/html", "utf-8")
    }

    private companion object {
        const val REQ_PAIR = 1
        const val REQ_LOCATION = 2
        const val REQ_NOTIF = 3
        const val REQ_BACKGROUND = 4
    }
}
