package com.jbrain.dashboard

import java.util.Base64
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONArray
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class LocationPublisherTest {
    private lateinit var server: MockWebServer
    private val publisher = LocationPublisher()
    private val report = LocationReport(40.0, -74.0, 1_700_000_000, accuracyM = 9)
    private fun batch(vararg r: LocationReport) = r.toList()

    @Before fun start() { server = MockWebServer(); server.start() }
    @After fun stop() { server.shutdown() }

    private fun base() = server.url("/").toString().trimEnd('/')

    @Test
    fun postsAJsonArrayWithTheKeyAsBasicPasswordAndAcksOn200() {
        server.enqueue(MockResponse().setResponseCode(200).setBody("[]"))

        val second = LocationReport(41.0, -75.0, 1_700_000_001)
        assertEquals(
            PublishOutcome.Published,
            publisher.publishBatch(base(), "dev-key", batch(report, second)),
        )

        val request = server.takeRequest()
        assertEquals("/api/owntracks", request.path)
        // The device key is the Basic password (username ignored by the server).
        val creds = String(Base64.getDecoder().decode(request.getHeader("Authorization")!!.removePrefix("Basic ")))
        assertEquals("dev-key", creds.substringAfter(":"))
        // The body is a JSON array of `_type:location` objects, oldest first.
        val sent = JSONArray(request.body.readUtf8())
        assertEquals(2, sent.length())
        assertEquals("location", sent.getJSONObject(0).getString("_type"))
        assertEquals(40.0, sent.getJSONObject(0).getDouble("lat"), 0.0)
        assertEquals(41.0, sent.getJSONObject(1).getDouble("lat"), 0.0)
    }

    @Test
    fun a401IsUnauthorized() {
        server.enqueue(MockResponse().setResponseCode(401))
        assertEquals(PublishOutcome.Unauthorized, publisher.publishBatch(base(), "k", batch(report)))
    }

    @Test
    fun a429IsRateLimited() {
        server.enqueue(MockResponse().setResponseCode(429))
        assertEquals(PublishOutcome.RateLimited, publisher.publishBatch(base(), "k", batch(report)))
    }

    @Test
    fun anUnexpectedStatusIsAFailure() {
        server.enqueue(MockResponse().setResponseCode(500))
        assertTrue(publisher.publishBatch(base(), "k", batch(report)) is PublishOutcome.Failed)
    }
}
