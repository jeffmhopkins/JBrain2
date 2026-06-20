package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

private class RecordingStore : CredentialStore {
    var key: String? = null
    var config: String? = null
    override fun deviceKey(): String? = key
    override fun owntracksConfig(): String? = config
    override fun save(deviceKey: String, owntracksConfig: String) {
        key = deviceKey
        config = owntracksConfig
    }
    override fun clear() { key = null; config = null }
}

class PairingCoordinatorTest {
    private fun coordinatorWith(store: CredentialStore, outcome: RedeemOutcome) =
        PairingCoordinator(store, object : Redeemer {
            override fun redeem(serverBase: String, code: String) = outcome
        })

    @Test
    fun successPersistsTheKeyAndConfig() {
        val store = RecordingStore()
        val result = coordinatorWith(store, RedeemOutcome.Success("k", "{\"host\":\"b\"}")).pair("https://h", " code ")
        assertEquals(PairResult.Paired, result)
        assertEquals("k", store.key)
        assertEquals("{\"host\":\"b\"}", store.config)
    }

    @Test
    fun anInvalidCodePersistsNothing() {
        val store = RecordingStore()
        assertEquals(PairResult.BadCode, coordinatorWith(store, RedeemOutcome.Invalid).pair("https://h", "x"))
        assertNull(store.key)
    }

    @Test
    fun rateLimitedIsSurfaced() {
        assertEquals(
            PairResult.RateLimited,
            coordinatorWith(RecordingStore(), RedeemOutcome.RateLimited).pair("https://h", "x"),
        )
    }

    @Test
    fun aTransientFailureIsAnError() {
        val result = coordinatorWith(RecordingStore(), RedeemOutcome.Failed("timeout")).pair("https://h", "x")
        assertEquals(PairResult.Error("timeout"), result)
    }
}
