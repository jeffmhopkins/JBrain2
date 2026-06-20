package com.jbrain.dashboard

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NavigationPolicyTest {
    private val base = "https://dash.example"

    @Test
    fun allowsTheSameOriginIncludingSubpaths() {
        assertTrue(NavigationPolicy.sameOrigin(base, "https://dash.example/dash"))
        assertTrue(NavigationPolicy.sameOrigin(base, "https://dash.example/dash#map"))
        // Default https port is implicit — an explicit :443 is the same origin.
        assertTrue(NavigationPolicy.sameOrigin(base, "https://dash.example:443/x"))
    }

    @Test
    fun blocksADifferentHost() {
        assertFalse(NavigationPolicy.sameOrigin(base, "https://evil.example/dash"))
    }

    @Test
    fun blocksADifferentSchemeOrPort() {
        assertFalse(NavigationPolicy.sameOrigin(base, "http://dash.example/dash")) // scheme
        assertFalse(NavigationPolicy.sameOrigin(base, "https://dash.example:8443/x")) // port
    }

    @Test
    fun blocksOtherSchemesAndMalformedUrls() {
        assertFalse(NavigationPolicy.sameOrigin(base, "javascript:alert(1)"))
        assertFalse(NavigationPolicy.sameOrigin(base, "intent://evil#Intent;end"))
        assertFalse(NavigationPolicy.sameOrigin(base, "not a url"))
    }
}
