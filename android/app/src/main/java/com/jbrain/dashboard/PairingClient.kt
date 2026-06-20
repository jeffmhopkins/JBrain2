package com.jbrain.dashboard

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

/** The result of redeeming a one-time pairing code. */
sealed interface RedeemOutcome {
    /** The device is provisioned: [deviceKey] mints sessions; [owntracksConfig] is
     * the broker config the location engine uses (M5d). */
    data class Success(val deviceKey: String, val owntracksConfig: String) : RedeemOutcome

    /** 400: the code is wrong, expired, or already used. */
    data object Invalid : RedeemOutcome

    /** 429: too many attempts from this client — back off and retry. */
    data object RateLimited : RedeemOutcome

    /** A network error or unexpected status — transient. */
    data class Failed(val reason: String) : RedeemOutcome
}

/** Redeems a pairing code into device credentials. An interface so the pairing
 * flow is unit-testable with a canned outcome. */
interface Redeemer {
    fun redeem(serverBase: String, code: String): RedeemOutcome
}

/** Posts a pairing code to `/api/pairing/redeem` and extracts the device key (the
 * OwnTracks config's `password`) and the config itself. Free of Android types, so
 * it runs under JVM unit tests against MockWebServer. */
class PairingClient(private val client: OkHttpClient = OkHttpClient()) : Redeemer {
    override fun redeem(serverBase: String, code: String): RedeemOutcome {
        val url = "${serverBase.trimEnd('/')}/api/pairing/redeem"
        val payload = JSONObject().put("code", code).toString()
        val request = Request.Builder().url(url).post(payload.toRequestBody(JSON)).build()
        return try {
            client.newCall(request).execute().use { resp ->
                when (resp.code) {
                    200 -> parse(resp.body?.string())
                    400 -> RedeemOutcome.Invalid
                    429 -> RedeemOutcome.RateLimited
                    else -> RedeemOutcome.Failed("unexpected status ${resp.code}")
                }
            }
        } catch (e: Exception) {
            RedeemOutcome.Failed(e.message ?: "network error")
        }
    }

    private fun parse(body: String?): RedeemOutcome {
        if (body == null) return RedeemOutcome.Failed("empty redeem response")
        return try {
            val config = JSONObject(body).getJSONObject("config")
            val key = config.getString("password")
            RedeemOutcome.Success(key, config.toString())
        } catch (e: Exception) {
            RedeemOutcome.Failed("malformed redeem response")
        }
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
