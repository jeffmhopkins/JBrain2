package com.jbrain.dashboard

import java.util.Base64
import org.json.JSONObject

/** The self-contained pairing string the owner shares (paste or QR). It embeds the
 * server URL alongside the one-time code, so the app learns where to redeem (and
 * operate) from the code itself — nothing is baked into the build. Mirrors the
 * backend's base64url(JSON {v,u,c}); a malformed or unknown-version payload parses
 * to null. Pure (java.util + org.json), so it is JVM-unit-tested. */
object PairingPayload {
    const val VERSION = 1

    data class Parsed(val serverBase: String, val code: String)

    fun parse(raw: String): Parsed? {
        val s = raw.trim()
        return try {
            val padded = s + "=".repeat((4 - s.length % 4) % 4)
            val json = JSONObject(String(Base64.getUrlDecoder().decode(padded), Charsets.UTF_8))
            if (json.optInt("v") != VERSION) return null
            val url = json.optString("u")
            val code = json.optString("c")
            if (url.isBlank() || code.isBlank()) null else Parsed(url, code)
        } catch (e: Exception) {
            null
        }
    }
}
