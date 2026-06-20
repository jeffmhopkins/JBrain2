package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

private class FakeStore(server: String?, initialKey: String?) : CredentialStore {
    var server: String? = server
    var key: String? = initialKey
    var config: String? = if (initialKey == null) null else "{}"
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

class SessionLauncherTest {
    // The launcher consumes only the Minter interface, so a canned outcome stands
    // in for the network without a socket.
    private fun launcherWith(store: CredentialStore, outcome: MintOutcome) =
        SessionLauncher(store, object : Minter {
            override fun mint(serverBase: String, deviceKey: String) = outcome
        })

    @Test
    fun noStoredServerOrKeyRoutesToPairing() {
        assertEquals(
            LaunchDecision.NeedsPairing,
            launcherWith(FakeStore(null, null), MintOutcome.Unauthorized).launch(),
        )
        // A key but no server (or vice versa) is still "not paired".
        assertEquals(
            LaunchDecision.NeedsPairing,
            launcherWith(FakeStore(null, "k"), MintOutcome.Success("c")).launch(),
        )
    }

    @Test
    fun successLoadsThePairedServersDash() {
        val store = FakeStore("https://h.example", "k")
        val decision = launcherWith(store, MintOutcome.Success("c=1")).launch()
        assertTrue(decision is LaunchDecision.Load)
        decision as LaunchDecision.Load
        assertEquals("https://h.example/dash", decision.url)
        assertEquals("c=1", decision.setCookie)
    }

    @Test
    fun aRevokedKeyIsClearedAndRoutesToPairing() {
        val store = FakeStore("https://h.example", "stale-key")
        val decision = launcherWith(store, MintOutcome.Unauthorized).launch()
        assertEquals(LaunchDecision.NeedsPairing, decision)
        assertNull(store.key) // self-healed: the dead key is gone
        assertNull(store.server)
    }

    @Test
    fun aTransientFailureKeepsTheKeyForRetry() {
        val store = FakeStore("https://h.example", "good-key")
        val decision = launcherWith(store, MintOutcome.Failed("timeout")).launch()
        assertTrue(decision is LaunchDecision.Retry)
        assertEquals("good-key", store.key) // not unpaired on a blip
    }
}
