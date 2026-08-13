package com.thebrokenrim.inkwell

import android.os.Bundle
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

class MainActivity : TauriActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Android 16 (API 36) removed the edge-to-edge opt-out entirely --
        // setDecorFitsSystemWindows() is now silently ignored, and Android
        // WebView never exposes status/navigation bar insets through CSS
        // env(safe-area-inset-*) regardless -- only display-cutout insets
        // get reflected there. Has to be handled natively instead: apply
        // the real system bar sizes as padding on the root content view,
        // which pushes Tauri's WebView in from the edges to match.
        val rootView = findViewById<android.view.View>(android.R.id.content)
        ViewCompat.setOnApplyWindowInsetsListener(rootView) { view, insets ->
            val systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(systemBars.left, systemBars.top, systemBars.right, systemBars.bottom)
            insets
        }
    }
}
