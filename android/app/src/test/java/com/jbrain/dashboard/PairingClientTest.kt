package com.jbrain.dashboard

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class PairingClientTest {
    private lateinit var server: MockWebServer
    private val client = PairingClient()

    @Before fun start() { server = MockWebServer(); server.start() }
    @After fun stop() { server.shutdown() }

    private fun base() = server.url("/").toString().trimEnd('/')

    @Test
    fun redeemsACodeIntoTheDeviceKeyAndConfig() {
        // The redeem response wraps the device key as the OwnTracks config password.
        val config = JSONObject().put("password", "dev-secret").put("host", "broker.example")
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody(JSONObject().put("config", config).put("dashboard_url", "https://h/dash").toString()),
        )

        val outcome = client.redeem(base(), "ABCD-1234")

        assertTrue(outcome is RedeemOutcome.Success)
        outcome as RedeemOutcome.Success
        assertEquals("dev-secret", outcome.deviceKey)
        assertTrue(outcome.owntracksConfig.contains("broker.example"))
        val request = server.takeRequest()
        assertEquals("/api/pairing/redeem", request.path)
        assertEquals("ABCD-1234", JSONObject(request.body.readUtf8()).getString("code"))
    }

    @Test
    fun a400IsAnInvalidCode() {
        server.enqueue(MockResponse().setResponseCode(400))
        assertEquals(RedeemOutcome.Invalid, client.redeem(base(), "nope"))
    }

    @Test
    fun a429IsRateLimited() {
        server.enqueue(MockResponse().setResponseCode(429))
        assertEquals(RedeemOutcome.RateLimited, client.redeem(base(), "x"))
    }

    @Test
    fun a200MissingThePasswordIsAFailure() {
        server.enqueue(
            MockResponse().setResponseCode(200)
                .setBody(JSONObject().put("config", JSONObject()).put("dashboard_url", "u").toString()),
        )
        assertTrue(client.redeem(base(), "x") is RedeemOutcome.Failed)
    }
}
