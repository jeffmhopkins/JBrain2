package com.jbrain.dashboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

private class FakeStore(initial: String?) : CredentialStore {
    var key: String? = initial
    override fun deviceKey(): String? = key
    override fun save(deviceKey: String) { key = deviceKey }
    override fun clear() { key = null }
}

class SessionLauncherTest {
    // The launcher consumes only the Minter interface, so a canned outcome stands
    // in for the network without a socket.
    private fun launcherWith(store: CredentialStore, outcome: MintOutcome) =
        SessionLauncher(store, object : Minter {
            override fun mint(serverBase: String, deviceKey: String) = outcome
        })

    @Test
    fun noStoredKeyRoutesToPairing() {
        val decision = launcherWith(FakeStore(null), MintOutcome.Unauthorized).launch("https://h.example")
        assertEquals(LaunchDecision.NeedsPairing, decision)
    }

    @Test
    fun successLoadsTheDashWithTheCookie() {
        val decision = launcherWith(FakeStore("k"), MintOutcome.Success("c=1")).launch("https://h.example")
        assertTrue(decision is LaunchDecision.Load)
        decision as LaunchDecision.Load
        assertEquals("https://h.example/dash", decision.url)
        assertEquals("c=1", decision.setCookie)
    }

    @Test
    fun aRevokedKeyIsClearedAndRoutesToPairing() {
        val store = FakeStore("stale-key")
        val decision = launcherWith(store, MintOutcome.Unauthorized).launch("https://h.example")
        assertEquals(LaunchDecision.NeedsPairing, decision)
        assertNull(store.key) // self-healed: the dead key is gone
    }

    @Test
    fun aTransientFailureKeepsTheKeyForRetry() {
        val store = FakeStore("good-key")
        val decision = launcherWith(store, MintOutcome.Failed("timeout")).launch("https://h.example")
        assertTrue(decision is LaunchDecision.Retry)
        assertEquals("good-key", store.key) // not unpaired on a blip
    }
}
