package com.jbrain.dashboard

import java.util.Base64
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PairingPayloadTest {
    private fun encode(json: String) = Base64.getUrlEncoder().withoutPadding().encodeToString(json.toByteArray())

    @Test
    fun parsesTheServerAndCode() {
        val parsed = PairingPayload.parse(encode("""{"v":1,"u":"https://hopkinsbrain.com","c":"CODE-1"}"""))
        assertEquals("https://hopkinsbrain.com", parsed?.serverBase)
        assertEquals("CODE-1", parsed?.code)
    }

    @Test
    fun toleratesWhitespaceAndMissingPadding() {
        // base64url without padding (as the backend emits) plus stray whitespace.
        val raw = encode("""{"v":1,"u":"https://h","c":"x"}""")
        assertEquals("https://h", PairingPayload.parse("  $raw  ")?.serverBase)
    }

    @Test
    fun rejectsAnUnknownVersion() {
        assertNull(PairingPayload.parse(encode("""{"v":2,"u":"https://h","c":"x"}""")))
    }

    @Test
    fun rejectsMissingFieldsAndGarbage() {
        assertNull(PairingPayload.parse(encode("""{"v":1,"u":"","c":"x"}""")))
        assertNull(PairingPayload.parse(encode("""{"v":1,"c":"x"}""")))
        assertNull(PairingPayload.parse("not base64 at all !!!"))
        assertNull(PairingPayload.parse(""))
    }
}
