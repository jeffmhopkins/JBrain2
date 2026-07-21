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

/** First-run setup for the owner app: capture the server URL to load. The owner signs in
 * through the web page itself (an owner key), so this only needs where to point — nothing
 * secret is entered here. Built programmatically (no XML), like PairingActivity. Returns
 * RESULT_OK once a valid https base is stored, so OwnerActivity launches into it. */
class OwnerSetupActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val gap = (resources.displayMetrics.density * 24).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(gap, gap, gap, gap)
        }
        val title = TextView(this).apply {
            text = "Connect to your JBrain"
            textSize = 22f
        }
        val input = EditText(this).apply { hint = "https://your-server" }
        val button = Button(this).apply { text = "Connect" }
        val status = TextView(this).apply { setPadding(0, gap / 2, 0, 0) }
        val wide = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)
        root.addView(title)
        root.addView(input, wide)
        root.addView(button, wide)
        root.addView(status)
        setContentView(root)

        button.setOnClickListener {
            val raw = input.text.toString().trim()
            if (!OwnerConfig.isValidBase(raw)) {
                status.text = "Enter a full https:// address."
                return@setOnClickListener
            }
            OwnerServerStore(this).save(raw)
            setResult(RESULT_OK)
            finish()
        }
    }
}
