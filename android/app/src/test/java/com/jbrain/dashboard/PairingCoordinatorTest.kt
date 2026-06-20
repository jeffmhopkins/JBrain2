package com.jbrain.dashboard

import java.util.Base64
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

private class RecordingStore : CredentialStore {
    var server: String? = null
    var key: String? = null
    var config: String? = null
    override fun serverBase(): String? = server
    override fun deviceKey(): String? = key
    override fun owntracksConfig(): String? = config
    override fun save(serverBase: String, deviceKey: String, owntracksConfig: String) {
        server = serverBase
        key = deviceKey
        config = owntracksConfig
    }
    override fun clear() {
        server = null
        key = null
        config = null
    }
}

/** A pairing payload (base64url JSON), as the backend mints it. */
private fun payload(url: String = "https://h.example", code: String = "code-1"): String {
    val json = """{"v":1,"u":"$url","c":"$code"}"""
    return Base64.getUrlEncoder().withoutPadding().encodeToString(json.toByteArray())
}

class PairingCoordinatorTest {
    private fun coordinatorWith(
        store: CredentialStore,
        outcome: RedeemOutcome,
        seen: (String, String) -> Unit = { _, _ -> },
    ) = PairingCoordinator(store, object : Redeemer {
        override fun redeem(serverBase: String, code: String): RedeemOutcome {
            seen(serverBase, code)
            return outcome
        }
    })

    @Test
    fun successPersistsTheServerKeyAndConfig() {
        val store = RecordingStore()
        var redeemedAt: Pair<String, String>? = null
        val coordinator = coordinatorWith(
            store,
            RedeemOutcome.Success("k", "{\"host\":\"b\"}"),
            seen = { s, c -> redeemedAt = s to c },
        )
        val result = coordinator.pair(payload(url = "https://h.example", code = "abc"))
        assertEquals(PairResult.Paired, result)
        // Redeemed at the server embedded in the payload, with its code.
        assertEquals("https://h.example" to "abc", redeemedAt)
        // And the server + key + config are all persisted.
        assertEquals("https://h.example", store.server)
        assertEquals("k", store.key)
        assertEquals("{\"host\":\"b\"}", store.config)
    }

    @Test
    fun aMalformedPayloadIsABadCodeAndNeverRedeems() {
        val store = RecordingStore()
        var redeemed = false
        val result = coordinatorWith(store, RedeemOutcome.Invalid, seen = { _, _ -> redeemed = true })
            .pair("not-a-real-payload")
        assertEquals(PairResult.BadCode, result)
        assertNull(store.server)
        assert(!redeemed) // parsing failed before any network call
    }

    @Test
    fun anInvalidCodePersistsNothing() {
        val store = RecordingStore()
        assertEquals(PairResult.BadCode, coordinatorWith(store, RedeemOutcome.Invalid).pair(payload()))
        assertNull(store.key)
    }

    @Test
    fun rateLimitedIsSurfaced() {
        assertEquals(
            PairResult.RateLimited,
            coordinatorWith(RecordingStore(), RedeemOutcome.RateLimited).pair(payload()),
        )
    }

    @Test
    fun aTransientFailureIsAnError() {
        val result = coordinatorWith(RecordingStore(), RedeemOutcome.Failed("timeout")).pair(payload())
        assertEquals(PairResult.Error("timeout"), result)
    }
}
