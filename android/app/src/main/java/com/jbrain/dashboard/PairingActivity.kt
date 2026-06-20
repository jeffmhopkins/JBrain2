package com.jbrain.dashboard

import android.app.Activity
import android.os.Bundle
import android.view.Gravity
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/** Pair-this-device screen: redeem a one-time code from the owner into stored
 * device credentials (M5c). The code is the only secret the user ever types; the
 * device key it yields is persisted in the Keystore store and never shown again.
 * Built programmatically (no XML) to keep the surface small. On success it returns
 * RESULT_OK so DashboardActivity re-launches into a real session.
 */
class PairingActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val gap = (resources.displayMetrics.density * 24).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(gap, gap, gap, gap)
        }
        val title = TextView(this).apply {
            text = "Pair this device"
            textSize = 22f
        }
        val input = EditText(this).apply { hint = "Paste your pairing code" }
        val button = Button(this).apply { text = "Pair" }
        val status = TextView(this).apply { setPadding(0, gap / 2, 0, 0) }
        val wide = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)
        root.addView(title)
        root.addView(input, wide)
        root.addView(button, wide)
        root.addView(status)
        setContentView(root)

        val coordinator = PairingCoordinator(KeystoreCredentialStore(this), PairingClient())
        button.setOnClickListener {
            val payload = input.text.toString()
            button.isEnabled = false
            status.text = "Pairing…"
            Thread {
                // The pasted/scanned string is the self-contained payload (server +
                // code); the coordinator parses it and learns the server from it.
                val result = coordinator.pair(payload)
                runOnUiThread {
                    button.isEnabled = true
                    when (result) {
                        PairResult.Paired -> {
                            setResult(RESULT_OK)
                            finish()
                        }
                        PairResult.BadCode -> status.text = "That code is invalid or expired."
                        PairResult.RateLimited -> status.text = "Too many tries — wait a moment."
                        is PairResult.Error -> status.text = "Couldn't reach the server."
                    }
                }
            }.start()
        }
    }
}
