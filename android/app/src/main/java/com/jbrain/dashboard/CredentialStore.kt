package com.jbrain.dashboard

/** The device key at rest. The key is the credential the app exchanges for a
 * session (it never reaches page JavaScript), so the implementation keeps it in
 * the Android Keystore-backed encrypted store. An interface so the mint/launch
 * flow is unit-testable with an in-memory fake. */
interface CredentialStore {
    /** The stored device key, or null when the device is not yet paired. */
    fun deviceKey(): String?

    fun save(deviceKey: String)

    /** Drop the key (on an Unauthorized mint — the key was revoked). */
    fun clear()
}
