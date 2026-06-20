package com.jbrain.dashboard

/** What the host should do on launch, decided from the stored key + a mint try. */
sealed interface LaunchDecision {
    /** Inject [setCookie] into the WebView jar, then load [url]. */
    data class Load(val url: String, val setCookie: String) : LaunchDecision

    /** No key, or the key was revoked — send the user to pairing (M5c). */
    data object NeedsPairing : LaunchDecision

    /** A transient mint failure — show a retry affordance, keep the key. */
    data class Retry(val reason: String) : LaunchDecision
}

/** Turns the device key into a ready-to-load dashboard session: read the key,
 * mint a cookie, and decide. A revoked key (401) self-heals by clearing the
 * store and routing to pairing; a transient failure keeps the key for a retry.
 * Pure logic — unit-tested with a fake store + MockWebServer-backed minter. */
class SessionLauncher(
    private val store: CredentialStore,
    private val minter: Minter,
) {
    fun launch(serverBase: String): LaunchDecision {
        val key = store.deviceKey() ?: return LaunchDecision.NeedsPairing
        return when (val outcome = minter.mint(serverBase, key)) {
            is MintOutcome.Success ->
                LaunchDecision.Load(DashboardConfig.dashboardUrl(serverBase), outcome.setCookie)
            MintOutcome.Unauthorized -> {
                store.clear()
                LaunchDecision.NeedsPairing
            }
            is MintOutcome.Failed -> LaunchDecision.Retry(outcome.reason)
        }
    }
}
