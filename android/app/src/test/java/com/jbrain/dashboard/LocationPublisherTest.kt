package com.jbrain.dashboard

import java.util.Base64
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class LocationPublisherTest {
    private lateinit var server: MockWebServer
    private val publisher = LocationPublisher()
    private val report = LocationReport(40.0, -74.0, 1_700_000_000, accuracyM = 9)

    @Before fun start() { server = MockWebServer(); server.start() }
    @After fun stop() { server.shutdown() }

    private fun base() = server.url("/").toString().trimEnd('/')

    @Test
    fun postsTheReportWithTheKeyAsBasicPasswordAndAcksOn200() {
        server.enqueue(MockResponse().setResponseCode(200).setBody("[]"))

        assertEquals(PublishOutcome.Published, publisher.publish(base(), "dev-key", report))

        val request = server.takeRequest()
        assertEquals("/api/owntracks", request.path)
        // The device key is the Basic password (username ignored by the server).
        val creds = String(Base64.getDecoder().decode(request.getHeader("Authorization")!!.removePrefix("Basic ")))
        assertEquals("dev-key", creds.substringAfter(":"))
        val sent = JSONObject(request.body.readUtf8())
        assertEquals("location", sent.getString("_type"))
        assertEquals(40.0, sent.getDouble("lat"), 0.0)
    }

    @Test
    fun a401IsUnauthorized() {
        server.enqueue(MockResponse().setResponseCode(401))
        assertEquals(PublishOutcome.Unauthorized, publisher.publish(base(), "k", report))
    }

    @Test
    fun a429IsRateLimited() {
        server.enqueue(MockResponse().setResponseCode(429))
        assertEquals(PublishOutcome.RateLimited, publisher.publish(base(), "k", report))
    }

    @Test
    fun anUnexpectedStatusIsAFailure() {
        server.enqueue(MockResponse().setResponseCode(500))
        assertTrue(publisher.publish(base(), "k", report) is PublishOutcome.Failed)
    }
}
