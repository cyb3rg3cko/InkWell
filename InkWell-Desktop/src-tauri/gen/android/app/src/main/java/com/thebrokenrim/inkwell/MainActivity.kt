package com.thebrokenrim.inkwell

import android.os.Bundle
import androidx.core.view.WindowCompat

class MainActivity : TauriActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Android 15+ makes edge-to-edge (content drawing underneath
        // the status bar and navigation bar) the default for apps
        // targeting SDK 35. This reverts to the pre-15 behavior --
        // the system automatically keeps content inset from the bars
        // instead of hiding parts of the UI behind them.
        WindowCompat.setDecorFitsSystemWindows(window, true)
    }
}
