package com.jbrain.dashboard

/** The outcome of a pairing attempt, surfaced to the pairing screen. */
sealed interface PairResult {
    /** Paired: the key + config are stored; the launcher can mint a session. */
    data object Paired : PairResult
    data object BadCode : PairResult
    data object RateLimited : PairResult
    data class Error(val reason: String) : PairResult
}

/** Redeems a code and, on success, persists the device key + OwnTracks config so a
 * later launch finds them. Pure logic — unit-tested with a fake store + redeemer. */
class PairingCoordinator(
    private val store: CredentialStore,
    private val redeemer: Redeemer,
) {
    fun pair(serverBase: String, code: String): PairResult =
        when (val outcome = redeemer.redeem(serverBase, code.trim())) {
            is RedeemOutcome.Success -> {
                store.save(outcome.deviceKey, outcome.owntracksConfig)
                PairResult.Paired
            }
            RedeemOutcome.Invalid -> PairResult.BadCode
            RedeemOutcome.RateLimited -> PairResult.RateLimited
            is RedeemOutcome.Failed -> PairResult.Error(outcome.reason)
        }
}
