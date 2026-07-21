package com.jbrain.dashboard

import android.content.Context

/** Where the owner app loads from, at rest. Unlike the member dashboard (whose device key
 * is a secret kept in the Keystore store), the owner signs in through the web page with an
 * owner key, so nothing sensitive lives here — just the server base. Plain
 * SharedPreferences is enough. */
class OwnerServerStore(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun base(): String? = prefs.getString(KEY_BASE, null)

    fun save(base: String) {
        prefs.edit().putString(KEY_BASE, base.trim().trimEnd('/')).apply()
    }

    private companion object {
        const val PREFS = "owner"
        const val KEY_BASE = "server_base"
    }
}
