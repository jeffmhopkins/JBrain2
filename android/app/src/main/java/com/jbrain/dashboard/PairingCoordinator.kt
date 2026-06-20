package com.jbrain.dashboard

/** The outcome of a pairing attempt, surfaced to the pairing screen. */
sealed interface PairResult {
    /** Paired: the key + config are stored; the launcher can mint a session. */
    data object Paired : PairResult
    data object BadCode : PairResult
    data object RateLimited : PairResult
    data class Error(val reason: String) : PairResult
}

/** Parses the self-contained pairing payload (server + code), redeems the code at
 * that server, and on success persists the server URL + device key + OwnTracks
 * config so every later call (mint, dashboard, location publish) targets the paired
 * server — no build-time URL. A payload that won't parse is a BadCode. Pure logic —
 * unit-tested with a fake store + redeemer. */
class PairingCoordinator(
    private val store: CredentialStore,
    private val redeemer: Redeemer,
) {
    fun pair(payload: String): PairResult {
        val parsed = PairingPayload.parse(payload) ?: return PairResult.BadCode
        return when (val outcome = redeemer.redeem(parsed.serverBase, parsed.code)) {
            is RedeemOutcome.Success -> {
                store.save(parsed.serverBase, outcome.deviceKey, outcome.owntracksConfig)
                PairResult.Paired
            }
            RedeemOutcome.Invalid -> PairResult.BadCode
            RedeemOutcome.RateLimited -> PairResult.RateLimited
            is RedeemOutcome.Failed -> PairResult.Error(outcome.reason)
        }
    }
}
