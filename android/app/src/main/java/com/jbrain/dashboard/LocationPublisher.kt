package com.jbrain.dashboard

import okhttp3.Credentials
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/** The result of publishing one location report. */
sealed interface PublishOutcome {
    data object Published : PublishOutcome

    /** 401: the device key was revoked — stop sharing and re-pair. */
    data object Unauthorized : PublishOutcome

    /** 429: the device is flooding — back off. */
    data object RateLimited : PublishOutcome

    /** A network error or unexpected status — transient. */
    data class Failed(val reason: String) : PublishOutcome
}

/** Publishes one location report to the server. An interface so the offline-queue
 * drain (LocationUploader) is unit-tested with a fake. */
interface Publisher {
    fun publish(serverBase: String, deviceKey: String, report: LocationReport): PublishOutcome
}

/** POSTs an OwnTracks location report to `/api/owntracks` with the device key as
 * the HTTP Basic password (the server ignores the username). The ingest acks 2xx
 * even on a transient downstream error, so only 401/429 are distinguished. Free of
 * Android types; JVM-tested against MockWebServer. */
class LocationPublisher(private val client: OkHttpClient = OkHttpClient()) : Publisher {
    override fun publish(serverBase: String, deviceKey: String, report: LocationReport): PublishOutcome {
        val url = "${serverBase.trimEnd('/')}/api/owntracks"
        val request = Request.Builder()
            .url(url)
            .header("Authorization", Credentials.basic("device", deviceKey))
            .post(report.toJson().toRequestBody(JSON))
            .build()
        return try {
            client.newCall(request).execute().use { resp ->
                when {
                    resp.isSuccessful -> PublishOutcome.Published
                    resp.code == 401 -> PublishOutcome.Unauthorized
                    resp.code == 429 -> PublishOutcome.RateLimited
                    else -> PublishOutcome.Failed("status ${resp.code}")
                }
            }
        } catch (e: Exception) {
            PublishOutcome.Failed(e.message ?: "network error")
        }
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
