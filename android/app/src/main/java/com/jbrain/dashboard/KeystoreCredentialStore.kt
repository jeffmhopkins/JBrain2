package com.jbrain.dashboard

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/** [CredentialStore] backed by EncryptedSharedPreferences — the device key is
 * encrypted with an AES master key held in the Android Keystore, so it is never
 * stored or readable in clear text (plan B8). Not JVM-unit-tested (it needs the
 * Android Keystore); the mint/launch logic is tested through the [CredentialStore]
 * interface with a fake. */
class KeystoreCredentialStore(context: Context) : CredentialStore {
    private val prefs = EncryptedSharedPreferences.create(
        context,
        "jbrain360-credentials",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    override fun deviceKey(): String? = prefs.getString(KEY, null)

    override fun owntracksConfig(): String? = prefs.getString(CONFIG, null)

    override fun save(deviceKey: String, owntracksConfig: String) {
        prefs.edit().putString(KEY, deviceKey).putString(CONFIG, owntracksConfig).apply()
    }

    override fun clear() {
        prefs.edit().remove(KEY).remove(CONFIG).apply()
    }

    private companion object {
        const val KEY = "device_key"
        const val CONFIG = "owntracks_config"
    }
}
