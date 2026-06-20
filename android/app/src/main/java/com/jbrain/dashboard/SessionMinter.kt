package com.jbrain.dashboard

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/** The result of exchanging the device key for a dashboard session. */
sealed interface MintOutcome {
    /** 204: [setCookie] is the raw `Set-Cookie` header to hand the WebView jar. */
    data class Success(val setCookie: String) : MintOutcome

    /** 401: the key is invalid / revoked — the stored key should be cleared and
     * the device re-paired. */
    data object Unauthorized : MintOutcome

    /** A network error or unexpected status — transient; retry, don't unpair. */
    data class Failed(val reason: String) : MintOutcome
}

/** Exchanges a device key for a dashboard session cookie. An interface so the
 * launch flow can be unit-tested with a canned outcome. */
interface Minter {
    fun mint(serverBase: String, deviceKey: String): MintOutcome
}

/** Posts the Keystore device key to `/api/session/mint` over TLS and returns the
 * dashboard session cookie (plan B8). Pure of Android types, so it runs under
 * plain JVM unit tests against MockWebServer. */
class SessionMinter(private val client: OkHttpClient = OkHttpClient()) : Minter {
    override fun mint(serverBase: String, deviceKey: String): MintOutcome {
        val url = "${serverBase.trimEnd('/')}/api/session/mint"
        val payload = JSONObject().put("device_key", deviceKey).toString()
        val request = Request.Builder()
            .url(url)
            .post(payload.toRequestBody(JSON))
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                when (resp.code) {
                    204 -> resp.header("Set-Cookie")
                        ?.let { MintOutcome.Success(it) }
                        ?: MintOutcome.Failed("no session cookie on the mint response")
                    401 -> MintOutcome.Unauthorized
                    else -> MintOutcome.Failed("unexpected status ${resp.code}")
                }
            }
        } catch (e: Exception) {
            MintOutcome.Failed(e.message ?: "network error")
        }
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
