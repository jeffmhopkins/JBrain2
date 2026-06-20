package com.jbrain.dashboard

/** The paired device's secrets + server at rest. The server base URL (learned from
 * the pairing payload, not baked into the build), the device key (exchanged for a
 * session, never reaching page JavaScript), and the one-time OwnTracks config are
 * all captured at pairing and kept in the Android Keystore-backed encrypted store.
 * An interface so the pairing + launch flow is unit-testable with an in-memory fake. */
interface CredentialStore {
    /** The paired server's base URL (e.g. https://host), or null when not yet paired. */
    fun serverBase(): String?

    /** The stored device key, or null when the device is not yet paired. */
    fun deviceKey(): String?

    /** The stored OwnTracks configuration JSON, or null when not yet paired. */
    fun owntracksConfig(): String?

    /** Persist the pairing result: the server URL (from the payload) + the device key
     * + the one-time OwnTracks config (the pairing code is single-use, so capture now). */
    fun save(serverBase: String, deviceKey: String, owntracksConfig: String)

    /** Drop the stored secrets (on an Unauthorized mint — the key was revoked). */
    fun clear()
}
