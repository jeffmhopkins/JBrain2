package com.jbrain.dashboard

import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class SessionMinterTest {
    private lateinit var server: MockWebServer
    private val minter = SessionMinter()

    @Before
    fun start() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun stop() {
        server.shutdown()
    }

    @Test
    fun postsTheKeyAndReturnsTheSessionCookieOn204() {
        server.enqueue(
            MockResponse().setResponseCode(204).addHeader("Set-Cookie", "jbrain_session=abc; Path=/"),
        )

        val outcome = minter.mint(server.url("/").toString().trimEnd('/'), "dev-key-1")

        assertTrue(outcome is MintOutcome.Success)
        assertEquals("jbrain_session=abc; Path=/", (outcome as MintOutcome.Success).setCookie)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/session/mint", request.path)
        // The body carries exactly the device key as JSON.
        assertEquals("dev-key-1", JSONObject(request.body.readUtf8()).getString("device_key"))
    }

    @Test
    fun mapsA401ToUnauthorized() {
        server.enqueue(MockResponse().setResponseCode(401))
        assertEquals(MintOutcome.Unauthorized, minter.mint(server.url("/").toString(), "bad"))
    }

    @Test
    fun a204WithoutACookieIsAFailureNotASuccess() {
        server.enqueue(MockResponse().setResponseCode(204))
        assertTrue(minter.mint(server.url("/").toString(), "k") is MintOutcome.Failed)
    }

    @Test
    fun anUnexpectedStatusIsAFailure() {
        server.enqueue(MockResponse().setResponseCode(503))
        assertTrue(minter.mint(server.url("/").toString(), "k") is MintOutcome.Failed)
    }
}
