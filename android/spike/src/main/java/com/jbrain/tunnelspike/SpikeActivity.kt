package com.jbrain.tunnelspike

import android.annotation.SuppressLint
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.ClientCertRequest
import android.webkit.HttpAuthHandler
import android.webkit.SslErrorHandler
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebStorage
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.webkit.ProxyConfig
import androidx.webkit.ProxyController
import androidx.webkit.ServiceWorkerClientCompat
import androidx.webkit.ServiceWorkerControllerCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.security.SecureRandom
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.concurrent.Executor
import kotlin.concurrent.thread

/** Runs the S0 checks of docs/plans/OWNER_TUNNEL_PLAN.md end to end on the phone and produces a
 * report to paste back. Two stages: the design (owner host, trusted through the network
 * security config) and the fallback (an untrusted cert accepted by SPKI in onReceivedSslError).
 *
 * A spike: deliberately one file of orchestration, no persistence, nothing reused by the app. */
class SpikeActivity : Activity() {
    private val main = Handler(Looper.getMainLooper())
    private val mainExecutor = Executor { main.post(it) }
    private val report = JSONObject()
    private val stages = listOf("owner", "fallback")
    private var stage = 0
    private var stageStarted = 0L

    private lateinit var web: WebView
    private lateinit var status: TextView
    private lateinit var key: DeviceKey
    private lateinit var proxy: ConnectProxy
    private lateinit var ownerServer: TestServer
    private lateinit var fallbackServer: TestServer
    private val proxyUser = "spike"
    private val proxyPass = randomToken()
    private var fallbackSpki = ""

    private val sslErrors = JSONArray()
    private val clientCertRequests = JSONArray()
    private val authRequests = JSONArray()
    private var swRequests = 0

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val useAuth = intent.getBooleanExtra(EXTRA_PROXY_AUTH, true)
        val useStrongBox = intent.getBooleanExtra(EXTRA_STRONGBOX, true)

        status = TextView(this).apply { text = "starting…"; setPadding(16, 16, 16, 16) }
        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
        }
        val buttons = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(button("Copy report") { copyReport() })
            addView(button("Rerun: no proxy auth") { rerun(proxyAuth = false, strongBox = useStrongBox) })
            addView(button("Rerun: TEE key") { rerun(proxyAuth = useAuth, strongBox = false) })
        }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(status, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(buttons, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(web, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        })

        report.put("device", JSONObject().apply {
            put("model", "${Build.MANUFACTURER} ${Build.MODEL}")
            put("sdk", Build.VERSION.SDK_INT)
            put("webview", WebViewCompat.getCurrentWebViewPackage(this@SpikeActivity)?.versionName ?: "unknown")
        })
        report.put("proxyAuthRequired", useAuth)

        thread {
            val setup = runCatching { startEverything(useAuth, useStrongBox) }
            main.post {
                setup.onFailure { fail("setup failed: $it") }.onSuccess { applyProxyThenLoad() }
            }
        }
    }

    private fun startEverything(useAuth: Boolean, useStrongBox: Boolean) {
        key = DeviceKey("tunnel-spike-device", useStrongBox)
        report.put("key", JSONObject().apply {
            put("strongBoxRequested", useStrongBox)
            put("strongBox", key.strongBox)
            put("securityLevel", key.securityLevel)
            put("signMillisAvg", "%.1f".format(key.signMillis()))
            put("spki", key.spki)
        })
        ownerServer = TestServer(resources.openRawResource(R.raw.spike_leaf), P12_PASSWORD).apply { start() }
        fallbackServer = TestServer(resources.openRawResource(R.raw.spike_fallback), P12_PASSWORD).apply { start() }
        fallbackSpki = fallbackLeafSpki()
        proxy = ConnectProxy(
            mapOf(OWNER to ownerServer.port, PINFAIL to ownerServer.port, FALLBACK to fallbackServer.port),
            proxyUser,
            proxyPass,
            useAuth,
        ).apply { start() }
    }

    private fun applyProxyThenLoad() {
        val features = JSONObject().apply {
            put("PROXY_OVERRIDE", WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE))
            put("PROXY_OVERRIDE_REVERSE_BYPASS", WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE_REVERSE_BYPASS))
            put("SERVICE_WORKER_BASIC_USAGE", WebViewFeature.isFeatureSupported(WebViewFeature.SERVICE_WORKER_BASIC_USAGE))
        }
        report.put("features", features)
        if (!features.getBoolean("PROXY_OVERRIDE") || !features.getBoolean("PROXY_OVERRIDE_REVERSE_BYPASS")) {
            fail("this WebView lacks proxy override or reverse bypass; that alone is the S0 answer")
            return
        }
        if (features.getBoolean("SERVICE_WORKER_BASIC_USAGE")) {
            ServiceWorkerControllerCompat.getInstance().setServiceWorkerClient(object : ServiceWorkerClientCompat() {
                override fun shouldInterceptRequest(request: WebResourceRequest): WebResourceResponse? {
                    swRequests += 1
                    return null
                }
            })
        }
        // Reverse bypass: ONLY the listed hosts go to the proxy; everything else goes direct.
        // setProxyOverride is process-wide, which is why the real app needs this rule.
        val config = ProxyConfig.Builder()
            .addProxyRule("127.0.0.1:${proxy.port}")
            .addBypassRule(OWNER)
            .addBypassRule(PINFAIL)
            .addBypassRule(FALLBACK)
            .setReverseBypassEnabled(true)
            .build()
        ProxyController.getInstance().setProxyOverride(config, mainExecutor) {
            WebStorage.getInstance().deleteAllData()
            web.clearSslPreferences()
            WebView.clearClientCertPreferences(null)
            web.webViewClient = Client()
            loadStage()
        }
    }

    private fun loadStage() {
        val name = stages[stage]
        status.text = "stage ${stage + 1}/${stages.size}: $name"
        stageStarted = System.currentTimeMillis()
        web.loadUrl("https://${if (name == "owner") OWNER else FALLBACK}/")
        pollResults()
    }

    private fun pollResults() {
        main.postDelayed({
            web.evaluateJavascript("JSON.stringify(window.__results || null)") { raw ->
                val parsed = runCatching { JSONObject(JSONArray("[$raw]").getString(0)) }.getOrNull()
                val timedOut = System.currentTimeMillis() - stageStarted > STAGE_TIMEOUT_MS
                if (parsed != null && parsed.optBoolean("done")) finishStage(parsed)
                else if (timedOut) finishStage(parsed ?: JSONObject().put("error", "page never reported (did it load?)"))
                else pollResults()
            }
        }, 500)
    }

    private fun finishStage(results: JSONObject) {
        val name = stages[stage]
        results.put("clientKeyMatches", results.optString("clientKeySeenByServer") == key.spki)
        report.put(name, results)
        stage += 1
        if (stage < stages.size) loadStage() else complete("done")
    }

    private fun complete(state: String) {
        report.put("callbacks", JSONObject().apply {
            put("sslErrors", sslErrors)
            put("clientCertRequests", clientCertRequests)
            put("httpAuthRequests", authRequests)
            put("serviceWorkerRequestsSeen", swRequests)
        })
        report.put("proxy", JSONObject().apply {
            put("targetsSeen", JSONArray(proxy.targetsSeen.toList()))
            put("authChallenges", proxy.challenges)
            put("refused", proxy.refused)
        })
        report.put("servers", JSONObject().apply {
            put("ownerHandshakes", ownerServer.handshakes)
            put("ownerLastError", ownerServer.lastError)
            put("fallbackHandshakes", fallbackServer.handshakes)
            put("fallbackLastError", fallbackServer.lastError)
        })
        report.put("state", state)
        status.text = "$state: tap Copy report and paste it into the thread"
    }

    private fun fail(message: String) {
        report.put("error", message)
        status.text = message
        if (::proxy.isInitialized) complete("failed") else report.put("state", "failed")
    }

    private inner class Client : WebViewClient() {
        override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
            val leaf = leafOf(error)
            val spki = leaf?.let { Http.spki(it) } ?: "unreadable"
            val accept = error.url.contains(FALLBACK) && spki == fallbackSpki
            sslErrors.put(JSONObject().apply {
                put("url", error.url)
                put("primaryError", error.primaryError)
                put("spki", spki)
                put("proceeded", accept)
            })
            if (accept) handler.proceed() else handler.cancel()
        }

        override fun onReceivedClientCertRequest(view: WebView, request: ClientCertRequest) {
            val start = System.nanoTime()
            clientCertRequests.put(JSONObject().apply {
                put("host", request.host)
                put("keyTypes", JSONArray(request.keyTypes?.toList() ?: emptyList<String>()))
            })
            request.proceed(key.privateKey, arrayOf(key.certificate))
            report.put("clientCertProceedMillis", (System.nanoTime() - start) / 1e6)
        }

        override fun onReceivedHttpAuthRequest(view: WebView, handler: HttpAuthHandler, host: String, realm: String) {
            authRequests.put(JSONObject().put("host", host).put("realm", realm))
            handler.proceed(proxyUser, proxyPass)
        }
    }

    /** The leaf the WebView rejected. `getX509Certificate` is API 29+; below that the same DER
     * sits in the certificate's saved state. */
    private fun leafOf(error: SslError): X509Certificate? = runCatching {
        if (Build.VERSION.SDK_INT >= 29) return@runCatching error.certificate.x509Certificate
        val der = android.net.http.SslCertificate.saveState(error.certificate).getByteArray("x509-certificate")
        der?.let { CertificateFactory.getInstance("X.509").generateCertificate(ByteArrayInputStream(it)) as X509Certificate }
    }.getOrNull()

    private fun fallbackLeafSpki(): String {
        val ks = java.security.KeyStore.getInstance("PKCS12")
        resources.openRawResource(R.raw.spike_fallback).use { ks.load(it, P12_PASSWORD) }
        return Http.spki(ks.getCertificate(ks.aliases().nextElement()) as X509Certificate)
    }

    private fun copyReport() {
        val clip = getSystemService(ClipboardManager::class.java)
        clip.setPrimaryClip(ClipData.newPlainText("tunnel spike report", report.toString(2)))
        Toast.makeText(this, "Report copied", Toast.LENGTH_SHORT).show()
    }

    private fun rerun(proxyAuth: Boolean, strongBox: Boolean) {
        finish()
        startActivity(
            Intent(this, SpikeActivity::class.java)
                .putExtra(EXTRA_PROXY_AUTH, proxyAuth)
                .putExtra(EXTRA_STRONGBOX, strongBox),
        )
    }

    private fun button(label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        textSize = 11f
        setOnClickListener { onClick() }
    }

    override fun onDestroy() {
        if (WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE)) {
            ProxyController.getInstance().clearProxyOverride(mainExecutor) {}
        }
        if (::proxy.isInitialized) proxy.stop()
        if (::ownerServer.isInitialized) ownerServer.stop()
        if (::fallbackServer.isInitialized) fallbackServer.stop()
        web.destroy()
        super.onDestroy()
    }

    companion object {
        const val OWNER = "owner.jbrain.home.arpa"
        const val PINFAIL = "pinfail.jbrain.home.arpa"
        const val FALLBACK = "fallback.jbrain.home.arpa"
        const val EXTRA_PROXY_AUTH = "proxyAuth"
        const val EXTRA_STRONGBOX = "strongBox"
        private const val STAGE_TIMEOUT_MS = 45_000L

        // The test p12s are throwaway spike material for names that exist nowhere; the
        // password guards nothing.
        private val P12_PASSWORD = "spike".toCharArray()

        private fun randomToken(): String =
            ByteArray(16).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
    }
}
