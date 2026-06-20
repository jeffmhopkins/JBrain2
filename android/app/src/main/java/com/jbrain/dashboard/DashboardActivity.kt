package com.jbrain.dashboard

import android.annotation.SuppressLint
import android.app.Activity
import android.os.Bundle
import android.webkit.WebView

/** Hosts the member dashboard SPA (served at /dash) in a locked-down WebView.
 *
 * The device key lives in the Android Keystore and is exchanged for the session
 * cookie natively (M5b); the WebView only renders the location-scoped dashboard.
 * No JavaScript bridge is registered, so page script can never reach native APIs.
 */
class DashboardActivity : Activity() {
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val web = WebView(this)
        web.settings.apply {
            javaScriptEnabled = true // the dashboard SPA is a React app
            domStorageEnabled = true // localStorage drives theme + font scale
            allowFileAccess = false // lockdown: no file:// reads
            allowContentAccess = false
        }
        setContentView(web)
        web.loadUrl(DashboardConfig.dashboardUrl(BuildConfig.DASHBOARD_BASE))
    }
}
