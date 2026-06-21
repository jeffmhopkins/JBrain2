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

/** Publishes a batch of location reports to the server in one request. An interface
 * so the offline-queue drain (LocationUploader) is unit-tested with a fake. */
interface Publisher {
    fun publishBatch(
        serverBase: String,
        deviceKey: String,
        reports: List<LocationReport>,
    ): PublishOutcome
}

/** POSTs a batch of OwnTracks location reports (a JSON array) to `/api/owntracks`
 * with the device key as the HTTP Basic password (the server ignores the username).
 * The ingest acks 2xx even on a transient downstream error, so only 401/429 are
 * distinguished. Free of Android types; JVM-tested against MockWebServer. */
class LocationPublisher(private val client: OkHttpClient = OkHttpClient()) : Publisher {
    override fun publishBatch(
        serverBase: String,
        deviceKey: String,
        reports: List<LocationReport>,
    ): PublishOutcome {
        val url = "${serverBase.trimEnd('/')}/api/owntracks"
        val request = Request.Builder()
            .url(url)
            .header("Authorization", Credentials.basic("device", deviceKey))
            .post(LocationReport.batchJson(reports).toRequestBody(JSON))
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
