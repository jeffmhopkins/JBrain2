package com.jbrain.tunnelspike

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream

class HttpTest {
    @Test
    fun readHeadStopsAtTheBlankLineAndLeavesTheRest() {
        val raw = "CONNECT owner.jbrain.home.arpa:443 HTTP/1.1\r\nHost: owner.jbrain.home.arpa:443\r\n" +
            "Proxy-Authorization: Basic abc\r\n\r\n\u0016\u0003\u0001"
        val input = ByteArrayInputStream(raw.toByteArray(Charsets.ISO_8859_1))
        val head = Http.readHead(input)!!
        assertEquals("CONNECT", head.method)
        assertEquals("owner.jbrain.home.arpa:443", head.target)
        assertEquals("Basic abc", head.headers["proxy-authorization"])
        // The TLS ClientHello bytes after the head are still unread, for the upstream.
        assertEquals(0x16, input.read())
    }

    @Test
    fun readHeadRefusesATruncatedOrMalformedHead() {
        assertNull(Http.readHead(ByteArrayInputStream("GET / HTTP/1.1\r\nHost: x".toByteArray())))
        assertNull(Http.parseHead("GARBAGE\r\n\r\n"))
        assertNull(Http.parseHead("GET / NOTHTTP\r\n\r\n"))
    }

    @Test
    fun connectTargetSplitsHostAndPort() {
        assertEquals("owner.jbrain.home.arpa" to 443, Http.connectTarget("Owner.JBrain.home.arpa:443"))
        assertNull(Http.connectTarget("owner.jbrain.home.arpa"))
        assertNull(Http.connectTarget("[::1]:443"))
        assertNull(Http.connectTarget("host:notaport"))
    }

    @Test
    fun basicAuthMatchesOnlyTheExactCredential() {
        val header = Http.basicHeader("spike", "s3cret")
        assertTrue(Http.basicMatches(header, "spike", "s3cret"))
        assertFalse(Http.basicMatches(header, "spike", "other"))
        assertFalse(Http.basicMatches(null, "spike", "s3cret"))
    }

    @Test
    fun websocketAcceptMatchesTheRfcExample() {
        // RFC 6455 §1.3.
        assertEquals("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", Http.websocketAccept("dGhlIHNhbXBsZSBub25jZQ=="))
    }

    @Test
    fun wsFrameReadsAMaskedClientTextFrame() {
        val payload = "hi".toByteArray()
        val mask = byteArrayOf(1, 2, 3, 4)
        val masked = ByteArray(payload.size) { (payload[it].toInt() xor mask[it % 4].toInt()).toByte() }
        val frame = byteArrayOf(0x81.toByte(), (0x80 or payload.size).toByte()) + mask + masked
        assertEquals("hi", WsFrame.readText(ByteArrayInputStream(frame)))
    }

    @Test
    fun wsFrameWritesAnUnmaskedServerTextFrame() {
        val out = ByteArrayOutputStream().apply { write(WsFrame.text("echo:hi")) }.toByteArray()
        assertEquals(0x81, out[0].toInt() and 0xFF)
        assertEquals(7, out[1].toInt())
        assertEquals("echo:hi", WsFrame.readText(ByteArrayInputStream(out)))
    }

    @Test
    fun spkiOfKnownBytes() {
        // SHA-256("") in base64, to pin the encoding the network_security_config <pin> uses.
        assertEquals("47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=", Http.spki(ByteArray(0)))
    }
}
